"""What actually went wrong behind an S3 call that failed.

botocore flattens very different failures into one message. A name that does
not resolve and a connection the server refused both read `Could not connect to
the endpoint URL`; the urllib3 error that tells them apart is kept in
`exc.kwargs["error"]` and in the exception chain, and printed nowhere. Without
it, an outage cannot be pinned on the DNS or on the network.
"""

from __future__ import annotations

import socket
from typing import Iterator, Optional

from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

# Every reason `classify` can return, so counters can start at zero for each.
REASONS = (
    "dns",
    "connect_timeout",
    "connect",
    "read_timeout",
    "http_5xx",
    "http_4xx",
    "other",
)

# Deep enough for botocore -> urllib3 -> socket, with room to spare; bounded so
# a chain that loops back on itself cannot hang the probe.
_MAX_CHAIN = 8


def underlying(exc: BaseException) -> Optional[BaseException]:
    """The error a botocore exception wraps, if it says."""
    kwargs = getattr(exc, "kwargs", None)
    if isinstance(kwargs, dict) and isinstance(kwargs.get("error"), BaseException):
        return kwargs["error"]
    if exc.__cause__ is not None:
        return exc.__cause__
    if exc.__context__ is not None and not exc.__suppress_context__:
        return exc.__context__
    return None


def chain(exc: BaseException) -> Iterator[BaseException]:
    """The exception, then each error it wraps, outermost first."""
    seen = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen and len(seen) < _MAX_CHAIN:
        seen.add(id(current))
        yield current
        current = underlying(current)


def describe(exc: BaseException) -> str:
    """`Type: message`, followed by the root cause botocore would have hidden.

    The root and not the next link: a source wraps botocore's error, which wraps
    urllib3's, and only the last one -- `gaierror`, `ConnectionRefusedError`,
    `TimeoutError` -- names the failure.
    """
    text = f"{type(exc).__name__}: {exc}"
    root = list(chain(exc))[-1]
    if root is not exc:
        text += f" (caused by {type(root).__name__}: {root})"
    return text


def _is_dns(exc: BaseException) -> bool:
    # urllib3 2.x raises NameResolutionError from the socket.gaierror; matched
    # by name so an urllib3 without it still falls back to the gaierror.
    return (
        isinstance(exc, socket.gaierror) or type(exc).__name__ == "NameResolutionError"
    )


def classify(exc: BaseException) -> str:
    """One of REASONS: where along the way the call failed."""
    if any(_is_dns(link) for link in chain(exc)):
        return "dns"
    if isinstance(exc, ConnectTimeoutError):
        return "connect_timeout"
    if isinstance(exc, ReadTimeoutError):
        return "read_timeout"
    if isinstance(exc, (EndpointConnectionError, ConnectionClosedError)):
        return "connect"
    if isinstance(exc, ClientError):
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status is None:
            # A HEAD reply has no body, so botocore puts the status in the code.
            code = str(exc.response.get("Error", {}).get("Code", ""))
            status = int(code) if code.isdigit() else None
        if status is not None and status >= 500:
            return "http_5xx"
        if status is not None and status >= 400:
            return "http_4xx"
    return "other"


# What S3 answers to a key it does not know or a signature it cannot match:
# the credential is wrong, not the storage. Worth reading the Secret again at
# once rather than at the next periodic refresh.
_CREDENTIAL_CODES = frozenset(
    {"InvalidAccessKeyId", "SignatureDoesNotMatch", "InvalidToken", "ExpiredToken"}
)


def is_credentials_error(exc: BaseException) -> bool:
    """True when S3 rejected the credential itself.

    A bare 403 is not enough: a policy that denies one call answers 403 just
    the same, and the probe already falls back on it.
    """
    for link in chain(exc):
        if isinstance(link, ClientError):
            code = str(link.response.get("Error", {}).get("Code", ""))
            if code in _CREDENTIAL_CODES:
                return True
    return False
