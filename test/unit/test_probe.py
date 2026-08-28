"""Connectivity probe: method selection, outcomes, and the /ready endpoint."""

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
    assert client.calls == ["head_bucket"]


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


# --- /ready ---------------------------------------------------------------


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
