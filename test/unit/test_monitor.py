"""One watched bucket: credentials over time, collections and their schedule."""

import logging

import pytest
from botocore.exceptions import ClientError

from exporter.buckets import BucketSpec
from exporter.credentials import CredentialsUnavailable, ResolvedCredentials
from exporter.monitor import (
    ADAPTIVE_FACTOR,
    MIN_REFRESH_GAP_SECONDS,
    BucketMonitor,
    MonitorSettings,
)
from exporter.sources.base import SourceUnavailable, Usage

A = ResolvedCredentials(access_key="key-a", secret_key="secret-a", bucket="uploads")
B = ResolvedCredentials(access_key="key-b", secret_key="secret-b", bucket="uploads")
OTHER = ResolvedCredentials(access_key="key-a", secret_key="secret-a", bucket="archive")


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class Credentials:
    """Answers each resolve() with the next queued value or error."""

    def __init__(self, *answers):
        self.answers = list(answers)

    def describe(self):
        return "secret app-a/app-a-s3"

    def resolve(self):
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


class Clients:
    """Records every client built, with the key it was built for."""

    def __init__(self, fail=False):
        self.built = []
        self.fail = fail

    def __call__(self, access_key, secret_key, region, endpoint_url, *timeout):
        if self.fail:
            raise ValueError("Invalid endpoint: nope")
        self.built.append((access_key, region, endpoint_url))
        return object()


class Source:
    """A usage source whose fetch takes `seconds` on the patched clock."""

    name = "list"

    def __init__(self, perf, seconds=1.0, error=None):
        self.perf = perf
        self.seconds = seconds
        self.error = error

    def fetch(self):
        self.perf.now += self.seconds
        if self.error:
            raise self.error
        return Usage(size_bytes=10, object_count=2)


def settings(**overrides):
    values = dict(
        interval_seconds=3600,
        retry_seconds=300,
        connectivity_interval=0,
        probe_timeout=5,
        max_failures=3,
        redetect_seconds=3600,
        source="list",
        sources_config={},
    )
    values.update(overrides)
    return MonitorSettings(**values)


def monitor(credentials, clock=None, clients=None, spec_overrides=None, **overrides):
    spec = BucketSpec(
        key="secret:app-a/app-a-s3",
        credentials=credentials,
        label_namespace="app-a",
        endpoint_url="https://grid:8082",
        region="us-east-1",
        **(spec_overrides or {}),
    )
    return BucketMonitor(
        spec,
        settings(**overrides),
        clock=clock or Clock(),
        rng=lambda: 0.5,
        build_client=clients or Clients(),
        build_probe=clients or Clients(),
    )


@pytest.fixture
def perf(monkeypatch):
    clock = Clock(0.0)
    monkeypatch.setattr("exporter.monitor.time.perf_counter", clock)
    return clock


def unavailable():
    return CredentialsUnavailable("reading secret app-a/app-a-s3 is forbidden")


# --- credentials over time -------------------------------------------------------


def test_readable_credentials_start_the_watch_at_once():
    clock, clients = Clock(), Clients()
    watched = monitor(Credentials(A), clock=clock, clients=clients)

    watched.refresh_credentials()

    assert watched.configured
    assert watched.bucket == "uploads"
    assert watched.next_due == clock.now
    assert clients.built[0] == ("key-a", "us-east-1", "https://grid:8082")
    assert watched.source_name == "list"


def test_an_unreadable_secret_leaves_the_bucket_unconfigured_then_recovers(caplog):
    watched = monitor(Credentials(unavailable(), unavailable(), A))

    with caplog.at_level(logging.DEBUG):
        watched.refresh_credentials()
        watched.refresh_credentials()

    assert not watched.configured
    assert "forbidden" in watched.error
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1  # the transition, not every attempt

    watched.refresh_credentials()
    assert watched.configured
    assert watched.error is None


def test_a_blip_keeps_the_credentials_in_hand(caplog):
    watched = monitor(Credentials(A, unavailable()))
    watched.refresh_credentials()

    with caplog.at_level(logging.WARNING):
        watched.refresh_credentials()

    assert watched.configured
    assert "keeping its last credentials" in caplog.text


def test_a_rotated_key_rebuilds_the_clients_and_keeps_the_figures():
    clients = Clients()
    watched = monitor(Credentials(A, B), clients=clients)
    watched.refresh_credentials()
    view = watched.view

    watched.refresh_credentials()

    assert watched.view is view
    assert ("key-b", "us-east-1", "https://grid:8082") in clients.built
    assert watched.credentials == B


def test_a_repointed_secret_is_a_new_bucket():
    watched = monitor(Credentials(A, OTHER))
    watched.refresh_credentials()
    view = watched.view

    watched.refresh_credentials()

    assert watched.view is not view
    assert watched.bucket == "archive"
    assert watched.view.bucket == "archive"


def test_a_client_that_cannot_be_built_leaves_the_bucket_unconfigured():
    watched = monitor(Credentials(A), clients=Clients(fail=True))

    watched.refresh_credentials()

    assert not watched.configured
    assert "cannot build the S3 client" in watched.error


def test_spread_starts_inside_the_first_interval():
    clock = Clock()
    watched = monitor(Credentials(A), clock=clock, spread=True)

    watched.refresh_credentials()

    # rng 0.5 of the first five minutes, not of the hour: a restart must not
    # leave a bucket without a size for an hour.
    assert watched.next_due == clock.now + 150


# --- collections ---------------------------------------------------------------


def started(clock=None, **kwargs):
    watched = monitor(Credentials(A), clock=clock, **kwargs)
    watched.refresh_credentials()
    return watched


def test_a_quick_collection_keeps_the_interval(perf):
    clock = Clock()
    watched = started(clock)
    watched.selector.source = Source(perf, seconds=2)

    watched.collect()

    assert watched.view.snapshot.healthy
    assert watched.next_due == clock.now + 3600
    assert watched.view.interval_seconds == 3600


def test_a_long_collection_stretches_the_interval(perf):
    clock = Clock()
    watched = started(clock)
    watched.selector.source = Source(perf, seconds=1800)

    watched.collect()

    assert watched.effective_interval == ADAPTIVE_FACTOR * 1800
    assert watched.next_due == clock.now + ADAPTIVE_FACTOR * 1800
    # Published, so the dashboard shows the interval actually in force.
    assert watched.view.interval_seconds == ADAPTIVE_FACTOR * 1800


def test_a_failed_collection_retries_sooner(perf):
    clock = Clock()
    watched = started(clock)
    watched.selector.source = Source(perf, error=SourceUnavailable("down"))

    watched.collect()

    assert not watched.view.snapshot.healthy
    assert watched.next_due == clock.now + 300


def test_a_per_bucket_interval_and_source_override_the_globals():
    watched = monitor(
        Credentials(A), spec_overrides={"interval_minutes": 120, "source": "list"}
    )
    assert watched.interval_seconds == 7200
    assert watched.requested_source == "list"


def test_a_probe_only_bucket_is_never_collected():
    watched = monitor(Credentials(A), spec_overrides={"collect": False})
    watched.refresh_credentials()

    assert watched.configured
    assert not watched.collectable()


def rejected_key():
    error = ClientError(
        {"Error": {"Code": "InvalidAccessKeyId", "Message": "x"}}, "ListObjectsV2"
    )
    try:
        raise SourceUnavailable("listing bucket 'uploads'") from error
    except SourceUnavailable as exc:
        return exc


def test_a_rejected_key_asks_for_the_secret_at_once(perf):
    clock = Clock()
    watched = started(clock)
    asked = []
    watched.on_refresh_request = lambda: asked.append(True)
    watched.selector.source = Source(perf, error=rejected_key())

    clock.now += MIN_REFRESH_GAP_SECONDS
    watched.collect()

    assert watched.needs_refresh
    assert asked == [True]


def test_rejected_keys_do_not_flood_the_api(perf):
    clock = Clock()
    watched = started(clock)
    asked = []
    watched.on_refresh_request = lambda: asked.append(True)
    watched.selector.source = Source(perf, error=rejected_key())

    watched.collect()  # right after the last read: not again

    assert asked == []


def test_an_outage_does_not_ask_for_the_secret(perf):
    clock = Clock()
    watched = started(clock)
    watched.on_refresh_request = lambda: pytest.fail("refresh requested")
    watched.selector.source = Source(perf, error=SourceUnavailable("timeout"))

    clock.now += MIN_REFRESH_GAP_SECONDS
    watched.collect()

    assert not watched.needs_refresh


# --- what it shows ------------------------------------------------------------


def labels_of(rows, name):
    return [labels for parts, labels, _, _ in rows if parts[-1] == name]


def test_an_unconfigured_bucket_shows_only_that():
    watched = monitor(Credentials(unavailable()))
    watched.refresh_credentials()

    rows = watched.rows()

    assert len(rows) == 1
    assert rows[0][1] == {
        "bucket_key": "secret:app-a/app-a-s3",
        "bucket_namespace": "app-a",
        "name": "",
    }
    assert rows[0][2] == 0
    assert watched.readiness()["state"] == "unconfigured"


def test_a_watched_bucket_shows_its_figures_under_bucket_namespace():
    watched = started()

    rows = watched.rows()

    assert rows[0][2] == 1
    size = labels_of(rows, "bucket_size_bytes")
    assert size == [{"bucket_namespace": "app-a", "name": "uploads"}]


def test_a_probe_only_bucket_shows_no_collection_figures():
    watched = monitor(
        Credentials(A), spec_overrides={"collect": False}, connectivity_interval=0
    )
    watched.refresh_credentials()

    names = {parts[-1] for parts, _, _, _ in watched.rows()}

    # A zero size would read as an empty bucket.
    assert "bucket_size_bytes" not in names
    assert "collect_interval_seconds" not in names
    assert "bucket_configured" in names


# --- removed by a reload ---------------------------------------------------------


def test_a_stopped_bucket_is_no_longer_collected_nor_started():
    watched = monitor(Credentials(unavailable(), A))
    watched.refresh_credentials()

    watched.stop()
    watched.refresh_credentials()

    assert not watched.configured  # a Secret readable now starts nothing
    assert not watched.collectable()


def test_a_collection_that_ends_after_the_stop_is_not_published(perf):
    clock = Clock()
    watched = started(clock)
    due = watched.next_due

    class StoppedMidway(Source):
        def fetch(self):
            watched.stop()
            return super().fetch()

    watched.selector.source = StoppedMidway(perf)
    watched.collect()

    # Not rescheduled: the scheduler never runs a removed bucket again.
    assert watched.next_due == due
    assert not watched.collectable()
