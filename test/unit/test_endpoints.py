"""Endpoint checks: DNS, then TCP, then TLS, once per endpoint."""

import json
import logging
import shutil
import socket
import ssl
import subprocess
import threading
import time

import falcon
import pytest

from exporter.endpoints import (
    CERT_WARNING_SECONDS,
    EndpointChecker,
    EndpointRegistry,
    _connect,
    endpoint_id,
)
from exporter.handler import ReadinessHandler

# --- identity ------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://grid.example:8082", "https://grid.example:8082"),
        ("https://Grid.Example/", "https://grid.example:443"),
        ("http://s3.storage.internal", "http://s3.storage.internal:80"),
        ("https://[2001:db8::1]:8082", "https://[2001:db8::1]:8082"),
        ("ftp://grid.example", None),
        ("https://", None),
        ("https://grid.example:notaport", None),
    ],
)
def test_an_endpoint_is_named_by_scheme_host_and_port(url, expected):
    assert endpoint_id(url) == expected


def test_without_an_endpoint_url_the_aws_one_is_worked_out(monkeypatch):
    monkeypatch.setattr(
        "exporter.endpoints.aws_endpoint",
        lambda region: f"https://s3.{region}.amazonaws.com",
    )
    assert endpoint_id("", "eu-west-1") == "https://s3.eu-west-1.amazonaws.com:443"


def test_the_aws_endpoint_comes_from_botocore():
    from exporter.endpoints import aws_endpoint

    assert aws_endpoint("eu-west-1") == "https://s3.eu-west-1.amazonaws.com"


# --- one cycle -------------------------------------------------------------------


class Sock:
    def __init__(self):
        self.closed = False
        self.timeout = None

    def settimeout(self, timeout):
        self.timeout = timeout

    def close(self):
        self.closed = True


class TlsSock(Sock):
    def __init__(self, cert):
        super().__init__()
        self.cert = cert
        self.unwrapped = False

    def getpeercert(self):
        return self.cert

    def unwrap(self):
        self.unwrapped = True


class Context:
    def __init__(self, cert=None, error=None):
        self.cert = cert if cert is not None else {}
        self.error = error
        self.hostnames = []

    def wrap_socket(self, sock, server_hostname):
        self.hostnames.append(server_hostname)
        if self.error:
            sock.close()
            raise self.error
        self.sock = TlsSock(self.cert)
        return self.sock


def infos(*addresses, port=8082):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses]


class Script:
    """Answers each call with the next queued value or error, the last one forever."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer() if callable(answer) else answer


def checker(
    endpoint="https://grid.example:8082",
    resolve=None,
    connect=None,
    context=None,
    clock=None,
):
    ctx = context or Context()
    return EndpointChecker(
        endpoint,
        30,
        5,
        resolve=resolve or Script(infos("10.0.0.2", "10.0.0.1")),
        connect=connect or Script(Sock),
        tls_context=lambda: ctx,
        clock=clock or (lambda: 0.0),
    )


def gaierror():
    return socket.gaierror(-3, "Temporary failure in name resolution")


def test_every_phase_passes_and_is_timed():
    context = Context()
    resolve = Script(infos("10.0.0.2", "10.0.0.1"))
    checked = checker(resolve=resolve, context=context)

    result = checked.check_once()

    assert result.up
    assert result.failed_phase is None
    assert result.addresses == ("10.0.0.1", "10.0.0.2")
    assert resolve.calls == [("grid.example", 8082)]
    assert context.hostnames == ["grid.example"]  # SNI and name check
    for phase in ("dns", "tcp", "tls"):
        assert getattr(result, f"{phase}_seconds") >= 0


def test_a_plain_http_endpoint_has_no_tls_phase():
    checked = checker("http://s3.storage.internal:8333")

    result = checked.check_once()

    assert result.up
    assert result.tls_seconds is None
    assert checked.phases == ("dns", "tcp")
    assert set(checked.failures_total) == {"dns", "tcp"}


def test_a_failed_resolution_stops_the_cycle_there():
    connect = Script(Sock)
    checked = checker(resolve=Script(gaierror()), connect=connect)

    result = checked.check_once()

    assert not result.up
    assert result.failed_phase == "dns"
    assert "gaierror" in result.error
    assert result.dns_seconds is not None  # how long it took to give up
    assert connect.calls == []
    assert checked.failures_total == {"dns": 1, "tcp": 0, "tls": 0}


def test_a_refused_connection_fails_at_tcp():
    checked = checker(connect=Script(ConnectionRefusedError(111, "Connection refused")))

    result = checked.check_once()

    assert result.failed_phase == "tcp"
    assert "ConnectionRefusedError" in result.error
    assert checked.failures_total["tcp"] == 1


def test_a_bad_certificate_fails_at_tls_and_closes_the_socket():
    sock = Sock()
    error = ssl.SSLCertVerificationError("certificate verify failed: expired")
    checked = checker(connect=Script(lambda: sock), context=Context(error=error))

    result = checked.check_once()

    assert result.failed_phase == "tls"
    assert "SSLCertVerificationError" in result.error
    assert sock.closed


def test_a_tls_connection_says_close_notify_then_closes():
    context = Context()
    checker(context=context).check_once()
    assert context.sock.unwrapped
    assert context.sock.closed


def test_a_plain_connection_is_closed_after_the_check():
    sock = Sock()
    checker("http://s3:80", connect=Script(lambda: sock)).check_once()
    assert sock.closed


def test_a_phase_not_reached_keeps_its_last_duration():
    checked = checker(resolve=Script(infos("10.0.0.1"), gaierror()))
    first = checked.check_once()

    second = checked.check_once()

    assert second.failed_phase == "dns"
    assert second.tcp_seconds == first.tcp_seconds
    assert second.tls_seconds == first.tls_seconds
    assert second.addresses == first.addresses


def test_the_certificate_end_is_read():
    context = Context(cert={"notAfter": "Jan  1 00:00:00 2030 GMT"})
    result = checker(context=context).check_once()
    assert result.cert_not_after == ssl.cert_time_to_seconds("Jan  1 00:00:00 2030 GMT")


def test_a_bad_ca_bundle_fails_at_tls_every_cycle_until_fixed(tmp_path):
    sock = Sock()
    checked = EndpointChecker(
        "https://grid:443",
        30,
        5,
        ca_bundle=str(tmp_path / "missing.pem"),
        resolve=Script(infos("10.0.0.1")),
        connect=Script(lambda: sock),
    )

    result = checked.check_once()

    assert result.failed_phase == "tls"
    assert sock.closed


# --- logs ------------------------------------------------------------------------


def test_a_failure_is_logged_once_and_the_recovery_too(caplog):
    checked = checker(resolve=Script(gaierror(), gaierror(), infos("10.0.0.1")))

    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            checked.check_once()

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "Endpoint https://grid.example:8082 failed at dns" in errors[0].message
    assert any("is reachable again" in r.message for r in caplog.records)


def test_a_change_of_address_is_logged(caplog):
    checked = checker(resolve=Script(infos("10.0.0.1"), infos("10.0.0.9")))
    checked.check_once()

    with caplog.at_level(logging.INFO):
        checked.check_once()

    assert any(
        "grid.example now resolves to 10.0.0.9 (was 10.0.0.1)" in r.message
        for r in caplog.records
    )


def test_a_certificate_close_to_its_end_is_warned_about_once_a_day(caplog):
    now = [ssl.cert_time_to_seconds("Jan  1 00:00:00 2030 GMT")]
    context = Context(cert={"notAfter": "Jan 11 00:00:00 2030 GMT"})
    checked = checker(context=context, clock=lambda: now[0])

    with caplog.at_level(logging.WARNING):
        checked.check_once()
        now[0] += 3600
        checked.check_once()
        now[0] += 24 * 3600
        checked.check_once()

    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "expires in 10.0 days" in warnings[0]


def test_a_distant_certificate_end_is_not_warned_about(caplog):
    now = ssl.cert_time_to_seconds("Jan  1 00:00:00 2030 GMT")
    context = Context(cert={"notAfter": "Jun  1 00:00:00 2030 GMT"})
    assert now + CERT_WARNING_SECONDS < ssl.cert_time_to_seconds(
        "Jun  1 00:00:00 2030 GMT"
    )

    with caplog.at_level(logging.WARNING):
        checker(context=context, clock=lambda: now).check_once()

    assert not caplog.records


# --- what it shows ---------------------------------------------------------------


def by_name(rows):
    shown = {}
    for parts, labels, value, _ in rows:
        shown.setdefault(parts[-1], []).append((labels, value))
    return shown


def test_before_the_first_check_only_the_counters_show():
    shown = by_name(checker().rows())

    assert set(shown) == {"endpoint_failures_total"}
    assert {labels["phase"] for labels, _ in shown["endpoint_failures_total"]} == {
        "dns",
        "tcp",
        "tls",
    }


def test_a_checked_endpoint_shows_every_phase():
    context = Context(cert={"notAfter": "Jan  1 00:00:00 2030 GMT"})
    checked = checker(context=context)
    checked.check_once()

    shown = by_name(checked.rows())

    assert shown["endpoint_up"] == [({"endpoint": "https://grid.example:8082"}, 1)]
    assert shown["endpoint_addresses"][0][1] == 2
    for name in (
        "endpoint_check_age_seconds",
        "endpoint_dns_duration_seconds",
        "endpoint_tcp_connect_seconds",
        "endpoint_tls_handshake_seconds",
        "endpoint_tls_cert_expiry_timestamp_seconds",
    ):
        assert name in shown


def test_a_down_endpoint_shows_zero_and_why_in_readyz():
    checked = checker(connect=Script(TimeoutError("timed out")))
    checked.check_once()

    assert by_name(checked.rows())["endpoint_up"][0][1] == 0
    state = checked.readiness()
    assert state["state"] == "down"
    assert state["phase"] == "tcp"
    assert "TimeoutError" in state["error"]


def test_an_endpoint_down_does_not_take_the_pod_out_of_service():
    resp = FakeResponse()
    handler = ReadinessHandler(
        lambda: {"a": {"name": "a", "state": "reachable"}},
        lambda: {"https://grid:443": {"state": "down", "phase": "tls"}},
    )

    handler.on_get(None, resp)

    assert resp.status == falcon.HTTP_200
    assert json.loads(resp.text)["endpoints"]["https://grid:443"]["phase"] == "tls"


class FakeResponse:
    def __init__(self):
        self.status = falcon.HTTP_200
        self.text = None

    def set_header(self, *_):
        pass


# --- the registry ----------------------------------------------------------------


class Started:
    def __init__(self, endpoint, interval, timeout, ca_bundle=None):
        self.endpoint = endpoint
        self.stop = None

    def start(self, stop, initial_delay=0):
        self.stop = stop
        self.initial_delay = initial_delay

    def rows(self):
        return [[["webtech_s3", "endpoint_up"], {"endpoint": self.endpoint}, 1, "h"]]

    def readiness(self):
        return {"state": "up"}


def registry(**kwargs):
    return EndpointRegistry(30, 5, ca_bundle="", factory=Started, **kwargs)


def test_a_hundred_buckets_on_one_grid_are_one_check():
    endpoints = registry()

    endpoints.sync(["https://grid:8082"] * 100 + [None])

    assert list(endpoints.checkers) == ["https://grid:8082"]
    assert len(endpoints.rows()) == 1


def test_sync_starts_the_new_and_stops_the_gone():
    endpoints = registry()
    endpoints.sync(["https://a:443", "https://b:443"])
    a = endpoints.checkers["https://a:443"]
    b = endpoints.checkers["https://b:443"]

    endpoints.sync(["https://b:443", "https://c:443"])

    assert a.stop.is_set()
    assert not b.stop.is_set()
    assert endpoints.checkers["https://b:443"] is b  # untouched, state kept
    assert set(endpoints.checkers) == {"https://b:443", "https://c:443"}


def test_first_checks_are_spread_with_many_buckets():
    endpoints = registry(spread=True, rng=lambda: 0.5)
    endpoints.sync(["https://a:443"])
    assert endpoints.checkers["https://a:443"].initial_delay == 15


def test_stop_stops_them_all():
    endpoints = registry()
    endpoints.sync(["https://a:443", "https://b:443"])

    endpoints.stop()

    assert all(c.stop.is_set() for c in endpoints.checkers.values())


# --- real sockets, on the loopback ------------------------------------------------


@pytest.fixture
def listener():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    yield server
    server.close()


def closed_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_the_next_address_is_tried_when_the_first_refuses(listener):
    port = listener.getsockname()[1]
    addresses = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", closed_port())),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
    ]

    sock = _connect(addresses, 2)

    assert sock.getpeername() == ("127.0.0.1", port)
    sock.close()


def test_every_address_refusing_raises_the_last_error():
    addresses = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", closed_port()))
    ]
    with pytest.raises(ConnectionRefusedError):
        _connect(addresses, 2)


@pytest.fixture
def tls_server(tmp_path):
    """A TLS server on the loopback, its certificate made by openssl."""
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not available to make a certificate")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "30",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    stop = threading.Event()

    def serve():
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                continue
            try:
                with context.wrap_socket(conn, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield server.getsockname()[1], str(cert)
    stop.set()
    thread.join(2)
    server.close()


def local(port):
    return lambda host, _, type=0: infos("127.0.0.1", port=port)


def test_a_real_handshake_against_a_trusted_certificate(tls_server, caplog):
    port, cert = tls_server
    checked = EndpointChecker(
        f"https://localhost:{port}", 30, 2, ca_bundle=cert, resolve=local(port)
    )

    with caplog.at_level(logging.WARNING):
        result = checked.check_once()

    assert result.up, result.error
    assert result.tls_seconds > 0
    assert result.cert_not_after is not None
    # 30 days left: more than the warning threshold of 14.
    assert not caplog.records


def test_a_real_handshake_against_an_untrusted_certificate(tls_server):
    port, _ = tls_server
    checked = EndpointChecker(f"https://localhost:{port}", 30, 2, resolve=local(port))

    result = checked.check_once()

    assert result.failed_phase == "tls"
    assert "certificate verify failed" in result.error


def test_building_the_tls_context_is_not_counted_as_the_handshake():
    def slow_context():
        time.sleep(0.2)  # loading the CAs
        return Context()

    checked = EndpointChecker(
        "https://grid:443",
        30,
        5,
        resolve=Script(infos("10.0.0.1")),
        connect=Script(Sock),
        tls_context=slow_context,
    )

    result = checked.check_once()

    assert result.up
    assert result.tls_seconds < 0.1
