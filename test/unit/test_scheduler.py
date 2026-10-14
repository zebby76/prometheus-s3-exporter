"""Collections of many buckets: who runs, in what order, and what is late."""

import threading
import time

from prometheus_client.core import InfoMetricFamily

from exporter.monitor import ExporterCollector
from exporter.scheduler import (
    MAX_SLEEP_SECONDS,
    OVERDUE_GRACE_SECONDS,
    CollectionScheduler,
    CredentialsRefresher,
)


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class Spec:
    def __init__(self, key):
        self.key = key
        self.label_namespace = "ns"


class Watched:
    def __init__(self, key, next_due, collect=True, configured=True):
        self.spec = Spec(key)
        self.next_due = next_due
        self.running = False
        self.collected = 0
        self._collect = collect
        self.configured = configured
        self.needs_refresh = False
        self.refreshed = 0

    def collectable(self):
        return self.configured and self._collect and self.next_due is not None

    def collect(self):
        self.collected += 1

    def refresh_credentials(self):
        self.refreshed += 1


class Deferred:
    """An executor that runs nothing until told to."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        self.jobs.append((fn, args))

    def run_all(self):
        jobs, self.jobs = self.jobs, []
        for fn, args in jobs:
            fn(*args)

    def shutdown(self, **kwargs):
        pass


def scheduler(monitors, workers=2, clock=None):
    executor = Deferred()
    return (
        CollectionScheduler(
            monitors,
            workers,
            threading.Event(),
            clock=clock or Clock(),
            executor=executor,
        ),
        executor,
    )


def test_the_most_overdue_runs_first_within_the_worker_limit():
    early, late, later, future = (
        Watched("a", 900),
        Watched("b", 500),
        Watched("c", 700),
        Watched("d", 5000),
    )
    sched, executor = scheduler([early, late, later, future], workers=2)

    sched.step()

    started = [args[0].spec.key for _, args in executor.jobs]
    assert started == ["b", "c"]
    assert sched.running() == 2
    assert not future.running


def test_a_finished_collection_frees_its_worker():
    a, b = Watched("a", 500), Watched("b", 600)
    sched, executor = scheduler([a, b], workers=1)

    sched.step()
    executor.run_all()
    a.next_due = 99999  # what collect() would set

    assert a.collected == 1
    assert sched.running() == 0
    sched.step()
    assert [args[0].spec.key for _, args in executor.jobs] == ["b"]


def test_unwatched_and_probe_only_buckets_are_never_collected():
    sched, executor = scheduler(
        [Watched("a", 500, configured=False), Watched("b", 500, collect=False)]
    )

    sched.step()

    assert executor.jobs == []


def test_it_sleeps_until_the_next_due_collection():
    clock = Clock(1000)
    sched, _ = scheduler([Watched("a", 1030)], clock=clock)

    assert sched.step() == 30


def test_it_never_sleeps_longer_than_its_cap():
    sched, _ = scheduler([Watched("a", 99999)])
    assert sched.step() == MAX_SLEEP_SECONDS
    sched, _ = scheduler([])
    assert sched.step() == MAX_SLEEP_SECONDS


def test_late_collections_with_no_worker_free_are_overdue():
    clock = Clock(10000)
    late = Watched("a", 10000 - OVERDUE_GRACE_SECONDS - 1)
    just_due = Watched("b", 10000)
    busy = Watched("c", 0)
    busy.running = True
    sched, _ = scheduler([late, just_due, busy], clock=clock)

    assert sched.overdue() == 1


def test_a_failing_collection_frees_its_worker_too():
    broken = Watched("a", 500)
    broken.collect = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    sched, executor = scheduler([broken], workers=1)

    sched.step()
    executor.run_all()

    assert sched.running() == 0
    assert not broken.running


# --- the credentials refresher --------------------------------------------------


def test_a_full_pass_reads_every_bucket_and_says_so():
    monitors = [Watched("a", None), Watched("b", None)]
    changed = []
    refresher = CredentialsRefresher(
        monitors, 300, threading.Event(), on_change=lambda: changed.append(1)
    )

    refresher.refresh_all()

    assert [m.refreshed for m in monitors] == [1, 1]
    assert changed == [1]


def test_a_request_reads_only_the_flagged_buckets():
    flagged, quiet = Watched("a", None), Watched("b", None)
    flagged.needs_refresh = True
    stop = threading.Event()
    refresher = CredentialsRefresher([flagged, quiet], 3600, stop)
    refresher.start()

    refresher.request()
    deadline = time.monotonic() + 5
    while flagged.refreshed == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    refresher.request()

    assert flagged.refreshed == 1
    assert quiet.refreshed == 0


# --- the exporter's metrics ------------------------------------------------------


class Shown(Watched):
    def __init__(self, key, bucket, configured=True, fail=False):
        super().__init__(key, 0, configured=configured)
        self.bucket = bucket if configured else None
        self.source_name = "list"
        self.endpoint = "https://grid:8082"
        self.fail = fail

    def rows(self):
        if self.fail:
            raise RuntimeError("broken")
        rows = [
            [
                ["webtech_s3", "bucket_configured"],
                {
                    "bucket_key": self.spec.key,
                    "bucket_namespace": "ns",
                    "name": self.bucket or "",
                },
                int(self.configured),
                "h",
            ]
        ]
        if self.configured:
            rows.append(
                [
                    ["webtech_s3", "bucket_size_bytes"],
                    {"bucket_namespace": "ns", "name": self.bucket},
                    10,
                    "h",
                ]
            )
        return rows


class SchedulerStub:
    def running(self):
        return 1

    def overdue(self):
        return 2


class EndpointsStub:
    def __init__(self, fail=False):
        self.fail = fail

    def rows(self):
        if self.fail:
            raise RuntimeError("broken")
        return [
            [
                ["webtech_s3", "endpoint_up"],
                {"endpoint": "https://grid:8082"},
                1,
                "h",
            ]
        ]


def families(monitors, endpoints=None):
    collector = ExporterCollector(monitors, SchedulerStub(), "6.0.0", endpoints)
    return {family.name: family for family in collector.collect()}


def test_every_bucket_lands_in_one_family_per_metric():
    shown = families([Shown("a", "uploads"), Shown("b", "archive")])

    size = shown["webtech_s3_bucket_size_bytes"]
    assert sorted(s.labels["name"] for s in size.samples) == ["archive", "uploads"]


def test_the_exporter_counts_its_buckets_and_collections():
    shown = families([Shown("a", "uploads"), Shown("b", None, configured=False)])

    states = {s.labels["state"]: s.value for s in shown["webtech_s3_buckets"].samples}
    assert states == {"configured": 1, "unconfigured": 1}
    assert shown["webtech_s3_collections_running"].samples[0].value == 1
    assert shown["webtech_s3_collections_overdue"].samples[0].value == 2


def test_info_has_one_series_per_watched_bucket():
    shown = families([Shown("a", "uploads"), Shown("b", None, configured=False)])

    info = shown["webtech_s3_exporter"]
    assert isinstance(info, InfoMetricFamily)
    (sample,) = info.samples
    assert sample.labels == {
        "bucket_namespace": "ns",
        "name": "uploads",
        "version": "6.0.0",
        "source": "list",
        "endpoint": "https://grid:8082",
    }


def test_one_broken_bucket_marks_the_exporter_down_but_shows_the_rest():
    shown = families([Shown("a", "uploads"), Shown("b", "x", fail=True)])

    assert shown["webtech_s3_exporter_up"].samples[0].value == 0
    names = [s.labels["name"] for s in shown["webtech_s3_bucket_size_bytes"].samples]
    assert names == ["uploads"]


def test_the_endpoints_are_shown_once_for_all_their_buckets():
    shown = families(
        [Shown("a", "uploads"), Shown("b", "archive")], endpoints=EndpointsStub()
    )

    (sample,) = shown["webtech_s3_endpoint_up"].samples
    assert sample.labels == {"endpoint": "https://grid:8082"}


def test_broken_endpoints_mark_the_exporter_down_but_keep_the_buckets():
    shown = families([Shown("a", "uploads")], endpoints=EndpointsStub(fail=True))

    assert shown["webtech_s3_exporter_up"].samples[0].value == 0
    assert "webtech_s3_bucket_size_bytes" in shown
