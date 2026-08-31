"""Access logs: the logfmt line the edge's parser expects."""

import email.message
import logging
import shlex
import time

import pytest

from exporter.handler import ACCESS_LOGGER_NAME, LogfmtRequestHandler, _access_logger


class Collector(logging.Handler):
    """Captures access records, which do not propagate to the root logger."""

    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@pytest.fixture
def access_lines():
    _access_logger()
    logger = logging.getLogger(ACCESS_LOGGER_NAME)
    collector = Collector()
    logger.addHandler(collector)
    try:
        yield collector.lines
    finally:
        logger.removeHandler(collector)


def request(path="/metrics", headers=None, command="GET"):
    """A handler positioned as it is when wsgiref calls log_request."""
    handler = object.__new__(LogfmtRequestHandler)
    handler.path = path
    handler.command = command
    handler.headers = email.message.Message()
    for name, value in (headers or {}).items():
        handler.headers[name] = value
    handler.client_address = ("10.42.0.1", 54321)
    handler._started = time.perf_counter()
    handler._rid = "0123456789abcdef"
    return handler


def parse(line):
    return dict(pair.split("=", 1) for pair in shlex.split(line))


def test_the_line_carries_the_edge_key_set(access_lines):
    request().log_request(200, 1234)

    assert list(parse(access_lines[0])) == [
        "ts",
        "rid",
        "class",
        "src",
        "status",
        "dur_us",
        "method",
        "uri",
        "qs",
        "target",
        "bytes",
        "host",
        "via",
        "vxid",
        "xff",
        "proto",
        "ua",
    ]


def test_a_scrape_is_reported_as_metrics_traffic(access_lines):
    request(
        headers={"Host": "exporter:9773", "User-agent": "Prometheus/2.53"}
    ).log_request(200, 1234)
    fields = parse(access_lines[0])

    assert fields["class"] == "metrics"
    assert fields["src"] == "exporter"
    assert fields["status"] == "200"
    assert fields["method"] == "GET"
    assert fields["bytes"] == "1234"
    assert fields["host"] == "exporter:9773"
    assert fields["ua"] == "Prometheus/2.53"
    assert int(fields["dur_us"]) >= 0


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/metrics", "metrics"),
        ("/healthz", "health"),
        ("/readyz", "health"),
        ("/", "other"),
    ],
)
def test_traffic_class_follows_the_path(access_lines, path, expected):
    request(path=path).log_request(200, 0)

    assert parse(access_lines[0])["class"] == expected


def test_the_query_string_is_split_from_the_target(access_lines):
    request(path="/metrics?name[]=webtech_s3_bucket_size").log_request(200, 12)
    fields = parse(access_lines[0])

    assert fields["uri"] == "/metrics?name[]=webtech_s3_bucket_size"
    assert fields["target"] == "/metrics"
    assert fields["qs"] == "name[]=webtech_s3_bucket_size"
    # Classification reads the path, so a query string must not defeat it.
    assert fields["class"] == "metrics"


def test_missing_headers_render_as_a_dash(access_lines):
    request().log_request(200, 0)
    fields = parse(access_lines[0])

    assert fields["host"] == "-"
    assert fields["via"] == "-"
    assert fields["vxid"] == "-"
    assert fields["xff"] == "-"
    assert fields["proto"] == "-"
    assert fields["ua"] == "-"


def test_an_empty_body_is_zero_bytes_not_a_dash(access_lines):
    # wsgiref passes "-", Apache's %B would say 0.
    request().log_request(304, "-")

    assert parse(access_lines[0])["bytes"] == "0"


def test_a_quote_in_a_header_does_not_break_the_line(access_lines):
    request(headers={"User-agent": 'curl "7.88"'}).log_request(200, 0)

    assert parse(access_lines[0])["ua"] == 'curl "7.88"'


def test_an_inbound_request_id_is_kept():
    assert request(headers={"X-Request-Id": "from-the-edge"})._request_id() == (
        "from-the-edge"
    )


def test_a_request_id_is_minted_when_the_edge_sent_none():
    minted = request()._request_id()

    assert len(minted) == 16
    assert int(minted, 16) >= 0


def test_the_edge_correlation_headers_are_passed_through(access_lines):
    request(
        headers={
            "X-Smals-Via": "apache",
            "X-Smals-Vxid": "987654",
            "X-Smals-Forwarded-For": "10.0.0.1, 10.0.0.2",
            "X-Smals-Forwarded-Proto": "https",
        }
    ).log_request(200, 0)
    fields = parse(access_lines[0])

    assert fields["via"] == "apache"
    assert fields["vxid"] == "987654"
    assert fields["xff"] == "10.0.0.1, 10.0.0.2"
    assert fields["proto"] == "https"
