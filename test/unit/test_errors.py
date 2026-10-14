"""Telling a DNS failure from a refused connection behind botocore's errors."""

import socket

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from exporter.errors import REASONS, classify, describe
from exporter.sources.base import SourceUnavailable

URL = "http://s3.storage.internal:8333/my-bucket"


class NameResolutionError(OSError):
    """Stands in for urllib3 2.x's, which botocore wraps on a failed lookup."""


class NewConnectionError(OSError):
    """Stands in for urllib3's, which botocore wraps on a refused connection."""


def wrapped(outer_factory, inner, root=None):
    """Raise the way botocore does: `outer(error=inner)` inside `except inner`.

    `root`, when given, is what `inner` was itself raised from, as urllib3 does
    with the socket error.
    """
    try:
        try:
            if root is not None:
                raise inner from root
            raise inner
        except Exception as caught:
            raise outer_factory(caught)
    except Exception as exc:
        return exc


def gaierror():
    return socket.gaierror(-3, "Temporary failure in name resolution")


def dns_failure():
    return wrapped(
        lambda e: EndpointConnectionError(endpoint_url=URL, error=e),
        NameResolutionError("Failed to resolve 's3.storage.internal'"),
        root=gaierror(),
    )


def refused():
    return wrapped(
        lambda e: EndpointConnectionError(endpoint_url=URL, error=e),
        NewConnectionError("Failed to establish a new connection"),
        root=ConnectionRefusedError(111, "Connection refused"),
    )


def client_error(status):
    return ClientError(
        {
            "Error": {"Code": str(status), "Message": "x"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "HeadBucket",
    )


def test_botocore_prints_the_same_message_for_both():
    # The reason this module exists.
    assert str(dns_failure()) == str(refused())


@pytest.mark.parametrize(
    "exc, reason",
    [
        (dns_failure(), "dns"),
        (refused(), "connect"),
        (
            wrapped(
                lambda e: ConnectTimeoutError(endpoint_url=URL, error=e),
                TimeoutError("timed out"),
            ),
            "connect_timeout",
        ),
        (
            wrapped(
                lambda e: ReadTimeoutError(endpoint_url=URL, error=e),
                TimeoutError("timed out"),
            ),
            "read_timeout",
        ),
        (
            wrapped(
                lambda e: ConnectionClosedError(endpoint_url=URL, error=e),
                ConnectionResetError(104, "Connection reset by peer"),
            ),
            "connect",
        ),
        (client_error(503), "http_5xx"),
        (client_error(403), "http_4xx"),
        (ValueError("something else"), "other"),
    ],
)
def test_each_failure_gets_its_reason(exc, reason):
    assert classify(exc) == reason
    assert reason in REASONS


def test_a_head_reply_without_metadata_is_read_from_its_code():
    # HEAD has no body: botocore puts the status where the code would be.
    exc = ClientError({"Error": {"Code": "503"}}, "HeadBucket")
    assert classify(exc) == "http_5xx"


def test_dns_is_found_even_wrapped_in_a_source_error():
    # The listing wraps botocore's error in its own, one level further out.
    try:
        raise SourceUnavailable("listing bucket 'b': boom") from dns_failure()
    except SourceUnavailable as exc:
        assert classify(exc) == "dns"


def test_the_description_names_the_root_cause():
    text = describe(dns_failure())

    assert text.startswith("EndpointConnectionError: Could not connect")
    assert "caused by gaierror" in text
    assert "Temporary failure in name resolution" in text


def test_a_refusal_reads_as_one():
    assert "caused by ConnectionRefusedError" in describe(refused())


def test_an_error_without_a_cause_is_described_as_is():
    assert describe(ValueError("plain")) == "ValueError: plain"


def test_a_looping_chain_does_not_hang():
    first = ValueError("a")
    second = ValueError("b")
    first.__cause__ = second
    second.__cause__ = first

    assert classify(first) == "other"
    assert describe(first).startswith("ValueError: a")
