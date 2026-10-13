"""Connectivity probe: method selection, outcomes, and the /readyz endpoint."""

import logging
import socket
import threading
from datetime import datetime, timedelta, timezone

import falcon
import pytest

from exporter.handler import ReadinessHandler
from exporter.probe import ConnectivityProbe, ProbeResult

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


class ClientError(Exception):
    """Stands in for botocore's ClientError, which carries `response`."""

    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeClient:
    """Records which calls were made, and can fail either of them."""

    def __init__(self, head_error=None, list_error=None):
        self.head_error = head_error
        self.list_error = list_error
        self.calls = []

    def head_bucket(self, **kwargs):
        self.calls.append("head_bucket")
        if self.head_error:
            raise self.head_error

    def list_objects_v2(self, **kwargs):
        self.calls.append(("list_objects_v2", kwargs.get("MaxKeys")))
        if self.list_error:
            raise self.list_error


def test_reachable_bucket_uses_head_bucket():
    client = FakeClient()
    result = ConnectivityProbe(client, "my-bucket", 60).check_once()

    assert result.reachable is True
    assert result.error is None
    assert client.calls == ["head_bucket"]


def test_head_bucket_denied_falls_back_to_a_one_key_listing():
    # A bucket-scoped policy may grant ListBucket but not HeadBucket.
    client = FakeClient(head_error=ClientError("AccessDenied"))
    result = ConnectivityProbe(client, "my-bucket", 60).check_once()

    assert result.reachable is True
    assert ("list_objects_v2", 1) in client.calls


def test_head_bucket_denied_without_a_body_falls_back_too():
    # A HEAD reply has no body for botocore to parse, so it reports the HTTP
    # status as the code. This is the shape production actually produces.
    client = FakeClient(head_error=ClientError("403"))
    result = ConnectivityProbe(client, "my-bucket", 60).check_once()

    assert result.reachable is True
    assert ("list_objects_v2", 1) in client.calls


def test_a_missing_bucket_is_still_an_outage():
    # 404 must not be read as a permission problem, or a bucket that vanished
    # would be probed forever through a listing that fails just as hard.
    client = FakeClient(head_error=ClientError("404"))
    result = ConnectivityProbe(client, "my-bucket", 60).check_once()

    assert result.reachable is False
    # Retried once, and never downgraded to the listing.
    assert client.calls == ["head_bucket", "head_bucket"]


def test_each_check_logs_its_round_trip_time(caplog):
    # Same shape as the collector's own line, so both are readable side by side.
    client = FakeClient()
    with caplog.at_level(logging.DEBUG):
        ConnectivityProbe(client, "my-bucket", 60).check_once()

    assert any(
        record.levelno == logging.DEBUG
        and "Probe S3 Bucket my-bucket via HeadBucket in" in record.message
        for record in caplog.records
    )


def test_the_fallback_is_named_in_the_timing_line(caplog):
    client = FakeClient(head_error=ClientError("403"))
    with caplog.at_level(logging.DEBUG):
        ConnectivityProbe(client, "my-bucket", 60).check_once()

    assert any(
        "Probe S3 Bucket my-bucket via ListObjectsV2 in" in record.message
        for record in caplog.records
    )


def test_a_failed_check_reports_how_long_it_took(caplog):
    client = FakeClient(head_error=ConnectionError("connection refused"))
    probe = ConnectivityProbe(client, "my-bucket", 60)
    probe._result = ProbeResult(reachable=True, checked_at=NOW)

    with caplog.at_level(logging.ERROR):
        probe.check_once()

    assert any(
        "became unreachable via HeadBucket after" in record.message
        for record in caplog.records
    )


def test_the_fallback_decision_is_remembered():
    client = FakeClient(head_error=ClientError("AccessDenied"))
    probe = ConnectivityProbe(client, "my-bucket", 60)
    probe.check_once()
    client.calls.clear()

    probe.check_once()

    # Re-testing HeadBucket on every pass would double the probe's cost.
    assert "head_bucket" not in client.calls


def test_a_real_outage_is_not_mistaken_for_a_permission_problem():
    client = FakeClient(head_error=ClientError("NoSuchBucket"))
    result = ConnectivityProbe(client, "my-bucket", 60).check_once()

    assert result.reachable is False
    assert "NoSuchBucket" in result.error
    # Never silently downgraded to the listing.
    assert client.calls == ["head_bucket", "head_bucket"]


def test_unreachable_bucket_reports_the_error():
    client = FakeClient(head_error=ConnectionError("connection refused"))
    result = ConnectivityProbe(client, "my-bucket", 60).check_once()

    assert result.reachable is False
    assert "connection refused" in result.error


def test_recovery_flips_the_result_back():
    client = FakeClient(head_error=ConnectionError("down"))
    probe = ConnectivityProbe(client, "my-bucket", 60)
    assert probe.check_once().reachable is False

    client.head_error = None
    assert probe.check_once().reachable is True


def test_rebuilt_client_is_adopted_and_the_method_re_decided():
    first = FakeClient(head_error=ClientError("AccessDenied"))
    probe = ConnectivityProbe(first, "my-bucket", 60)
    probe.check_once()

    second = FakeClient()
    probe.set_client(second)
    probe.check_once()

    assert "head_bucket" in second.calls


class StaleConnectionClient:
    """Fails the first call the way a dropped idle socket does, then works."""

    def __init__(self):
        self.calls = 0

    def head_bucket(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("Connection aborted, RemoteDisconnected")


def test_a_dropped_connection_is_retried_at_once_and_is_not_an_outage():
    # A load balancer that closes idle connections leaves a dead socket in the
    # pool. Reopening it costs one connection setup; calling that an outage
    # would take the pod out of service on a purely local matter.
    client = StaleConnectionClient()
    probe = ConnectivityProbe(client, "my-bucket", 30)

    result = probe.check_once()

    assert result.reachable is True
    assert result.attempts == 2
    assert client.calls == 2


def test_a_healthy_check_does_not_retry():
    probe = ConnectivityProbe(FakeClient(), "my-bucket", 30)

    result = probe.check_once()

    assert result.attempts == 1
    assert probe.retries_total == 0


def test_retries_are_counted_so_a_reconnection_is_visible():
    # The whole point of retrying here rather than in botocore: its backoff
    # would hide inside probe_duration_seconds as latency.
    probe = ConnectivityProbe(StaleConnectionClient(), "my-bucket", 30)
    probe.check_once()

    assert probe.retries_total == 1


def test_a_sustained_outage_gives_up_after_the_second_attempt():
    client = FakeClient(head_error=ConnectionError("connection refused"))
    probe = ConnectivityProbe(client, "my-bucket", 30)

    result = probe.check_once()

    assert result.reachable is False
    assert result.attempts == 2


def test_the_retry_counter_survives_a_client_rebuild():
    # It is monotonic on purpose: a counter that went backwards would break
    # increase() over it.
    probe = ConnectivityProbe(StaleConnectionClient(), "my-bucket", 30)
    probe.check_once()
    probe.set_client(StaleConnectionClient())
    probe.check_once()

    assert probe.retries_total == 2


def test_age_is_zero_before_the_first_probe():
    assert ProbeResult().age_seconds(now=NOW) == 0.0


def test_age_counts_from_the_last_check():
    result = ProbeResult(reachable=True, checked_at=NOW - timedelta(seconds=90))
    assert result.age_seconds(now=NOW) == 90


def test_a_disabled_probe_starts_no_thread():
    probe = ConnectivityProbe(FakeClient(), "my-bucket", 0)
    assert probe.start(threading.Event()) is None


def test_the_thread_stops_on_the_shared_event():
    probe = ConnectivityProbe(FakeClient(), "my-bucket", 60)
    stop = threading.Event()
    thread = probe.start(stop)

    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


# --- what failed, and where ------------------------------------------------


class SlowThenFailingClient:
    """First attempt fails after a while, the second at once, like production."""

    def __init__(self, clock, errors):
        self.clock = clock
        self.errors = list(errors)

    def head_bucket(self, **kwargs):
        error, seconds = self.errors.pop(0)
        self.clock.now += seconds
        raise error


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_a_failed_check_keeps_every_attempt(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("exporter.probe.time.perf_counter", clock)
    client = SlowThenFailingClient(
        clock,
        [(TimeoutError("connect timed out"), 5.0), (ConnectionError("refused"), 0.014)],
    )

    result = ConnectivityProbe(client, "my-bucket", 30).check_once()

    # The first attempt carried the five seconds; it used to go unlogged.
    assert result.error.startswith("#1 5.000s TimeoutError: connect timed out")
    assert "#2 0.014s ConnectionError: refused" in result.error
    assert result.reason == "other"


def test_every_failed_attempt_is_counted_by_reason():
    # Retried and recovered still counts: that is the hiccup worth seeing.
    probe = ConnectivityProbe(StaleConnectionClient(), "my-bucket", 30)
    probe.check_once()

    errors = probe.errors_total
    assert errors["other"] == 1
    assert sum(errors.values()) == 1


def test_a_recovered_retry_is_logged_at_info_with_its_cause(caplog):
    probe = ConnectivityProbe(StaleConnectionClient(), "my-bucket", 30)

    with caplog.at_level(logging.INFO):
        probe.check_once()

    assert any(
        record.levelno == logging.INFO
        and "(2 attempts): #1 failed in" in record.message
        and "RemoteDisconnected" in record.message
        for record in caplog.records
    )


def test_the_transition_line_names_the_reason(caplog):
    client = FakeClient(head_error=ConnectionError("refused"))
    probe = ConnectivityProbe(client, "my-bucket", 60)
    probe._result = ProbeResult(reachable=True, checked_at=NOW)

    with caplog.at_level(logging.ERROR):
        probe.check_once()

    assert any("2 attempts [other]: #1" in record.message for record in caplog.records)


# --- new connections ---------------------------------------------------------


class Socket:
    """Something a WeakSet can hold, as a real socket."""


class Connection:
    def __init__(self):
        self.sock = None


class PooledClient(FakeClient):
    """The botocore -> urllib3 attribute path, with one pooled connection.

    Each call reconnects when `drop` is set, the way urllib3 reopens a
    connection the server closed: a new socket under the same object.
    """

    def __init__(self):
        super().__init__()
        self.conn = Connection()
        self.drop = True
        queue = type("Queue", (), {"queue": [self.conn]})()
        pool = type("Pool", (), {"pool": queue})()
        manager = type("Manager", (), {"pools": {"key": pool}})()
        session = type("Session", (), {"_manager": manager})()
        self._endpoint = type("Endpoint", (), {"http_session": session})()

    def head_bucket(self, **kwargs):
        super().head_bucket(**kwargs)
        if self.drop or self.conn.sock is None:
            self.conn.sock = Socket()


def test_a_new_connection_is_counted_and_logged(caplog):
    probe = ConnectivityProbe(PooledClient(), "my-bucket", 30)

    with caplog.at_level(logging.INFO):
        probe.check_once()

    assert probe.connections_total == 1
    assert any("1 new connection" in record.message for record in caplog.records)


def test_a_reused_connection_stays_at_debug(caplog):
    client = PooledClient()
    probe = ConnectivityProbe(client, "my-bucket", 30)
    probe.check_once()
    client.drop = False

    with caplog.at_level(logging.DEBUG):
        caplog.clear()
        probe.check_once()

    timing = [r for r in caplog.records if "Probe S3 Bucket" in r.message]
    assert [r.levelno for r in timing] == [logging.DEBUG]
    assert probe.connections_total == 1


def test_a_connection_reopened_in_place_is_counted():
    # urllib3 reconnects a dropped connection under the same object: counting
    # connection objects missed every reconnection an idle timeout causes.
    client = PooledClient()
    probe = ConnectivityProbe(client, "my-bucket", 30)

    for _ in range(3):
        probe.check_once()

    assert probe.connections_total == 3


def test_the_connection_count_survives_a_client_rebuild():
    probe = ConnectivityProbe(PooledClient(), "my-bucket", 30)
    probe.check_once()
    probe.set_client(PooledClient())
    probe.check_once()

    assert probe.connections_total == 2


def test_connections_are_unknown_when_the_client_hides_them():
    probe = ConnectivityProbe(FakeClient(), "my-bucket", 30)
    probe.check_once()

    assert probe.connections_total is None


def test_a_real_boto3_client_exposes_its_pools():
    # Guards the private attribute path against a botocore upgrade.
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url="http://s3.example:8333",
        region_name="us-east-1",
        aws_access_key_id="a",
        aws_secret_access_key="b",
    )
    probe = ConnectivityProbe(client, "my-bucket", 30)
    assert probe._pooled_sockets() == []


# --- DNS ---------------------------------------------------------------------


class EndpointClient(FakeClient):
    def __init__(self, url):
        super().__init__()
        self.meta = type("Meta", (), {"endpoint_url": url})()


def fake_resolver(monkeypatch, answers):
    """getaddrinfo answering from `answers`, one item per call."""
    calls = []

    def getaddrinfo(host, port, type=0):
        calls.append((host, port))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return [(2, 1, 6, "", (address, port)) for address in answer]

    monkeypatch.setattr("exporter.probe.socket.getaddrinfo", getaddrinfo)
    return calls


def test_the_endpoint_name_is_resolved_and_timed(monkeypatch, caplog):
    calls = fake_resolver(monkeypatch, [["10.0.0.2", "10.0.0.1"]])
    probe = ConnectivityProbe(EndpointClient("http://s3.example:8333"), "b", 30)

    with caplog.at_level(logging.INFO):
        result = probe.resolve_once()

    assert calls == [("s3.example", 8333)]
    assert result.addresses == ("10.0.0.1", "10.0.0.2")
    assert result.error is None
    assert result.duration_seconds >= 0
    assert any(
        "s3.example resolves to 10.0.0.1, 10.0.0.2" in r.message for r in caplog.records
    )


def test_the_default_port_follows_the_scheme(monkeypatch):
    calls = fake_resolver(monkeypatch, [["10.0.0.1"]])
    ConnectivityProbe(EndpointClient("https://s3.example"), "b", 30).resolve_once()

    assert calls == [("s3.example", 443)]


def test_a_failed_resolution_is_counted_and_logged_once(monkeypatch, caplog):
    failure = socket.gaierror(-3, "Temporary failure in name resolution")
    fake_resolver(monkeypatch, [failure, failure, ["10.0.0.1"]])
    probe = ConnectivityProbe(EndpointClient("http://s3.example"), "b", 30)

    with caplog.at_level(logging.DEBUG):
        probe.resolve_once()
        probe.resolve_once()
        probe.resolve_once()

    assert probe.dns_failures_total == 2
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "Cannot resolve s3.example" in errors[0].message
    assert any("resolves again" in r.message for r in caplog.records)


def test_a_change_of_address_is_logged(monkeypatch, caplog):
    fake_resolver(monkeypatch, [["10.0.0.1"], ["10.0.0.9"]])
    probe = ConnectivityProbe(EndpointClient("http://s3.example"), "b", 30)
    probe.resolve_once()

    with caplog.at_level(logging.INFO):
        probe.resolve_once()

    assert any(
        "now resolves to 10.0.0.9 (was 10.0.0.1)" in r.message for r in caplog.records
    )


def test_no_endpoint_means_nothing_to_resolve(monkeypatch):
    calls = fake_resolver(monkeypatch, [])
    probe = ConnectivityProbe(FakeClient(), "b", 30)

    assert probe.resolve_once().checked_at is None
    assert calls == []


# --- /readyz ---------------------------------------------------------------


class StubProbe:
    def __init__(self, result):
        self.result = result


class FakeResponse:
    def __init__(self):
        self.status = falcon.HTTP_200
        self.text = None
        self.headers = {}

    def set_header(self, name, value):
        self.headers[name] = value


def ready(result):
    resp = FakeResponse()
    ReadinessHandler(StubProbe(result)).on_get(None, resp)
    return resp


def test_ready_is_200_when_the_bucket_is_reachable():
    resp = ready(ProbeResult(reachable=True, checked_at=NOW))
    assert resp.status == falcon.HTTP_200
    assert '"bucket": "reachable"' in resp.text


def test_ready_is_503_when_the_bucket_is_unreachable():
    resp = ready(ProbeResult(reachable=False, checked_at=NOW, error="boom"))
    assert resp.status == falcon.HTTP_503
    assert "boom" in resp.text


def test_ready_names_the_reason_of_the_failure():
    resp = ready(ProbeResult(reachable=False, checked_at=NOW, error="x", reason="dns"))
    assert '"reason": "dns"' in resp.text


def test_ready_is_200_before_the_first_probe():
    # Holding the pod out of service on no evidence would be worse than
    # letting it serve figures it already has.
    resp = ready(ProbeResult())
    assert resp.status == falcon.HTTP_200
    assert '"bucket": "unknown"' in resp.text


@pytest.mark.parametrize(
    "result",
    [
        ProbeResult(reachable=True, checked_at=NOW),
        ProbeResult(reachable=False, checked_at=NOW, error="boom"),
        ProbeResult(),
    ],
)
def test_ready_always_answers_json(result):
    assert ready(result).headers["Content-Type"] == "application/json"
