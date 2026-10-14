"""When the usage source is detected again.

An outage used to leave the exporter on the listing for good: detection ran
while the storage was down, every constant-cost source failed its probe, the
listing won, and nothing ever detected again once the storage was back.
"""

from datetime import datetime, timezone

from exporter.probe import ProbeResult
from exporter.selection import SourceSelector


class Named:
    def __init__(self, name):
        self.name = name


class Detector:
    """Returns the sources a test lines up, and counts the calls."""

    def __init__(self, *names):
        self.names = list(names)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return Named(self.names.pop(0) if len(self.names) > 1 else self.names[0])


class Probe:
    def __init__(self, reachable):
        self.result = ProbeResult(
            reachable=reachable, checked_at=datetime.now(timezone.utc)
        )


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_failures_while_the_bucket_is_unreachable_keep_the_source():
    detect = Detector("storagegrid", "list")
    selector = SourceSelector(detect, probe=Probe(reachable=False), max_failures=3)

    for _ in range(9):
        selector.record(False)

    assert selector.source.name == "storagegrid"
    assert detect.calls == 1


def test_failures_with_a_reachable_bucket_detect_again():
    # The storage answers but the source does not: that is the source's fault.
    detect = Detector("storagegrid", "list")
    selector = SourceSelector(detect, probe=Probe(reachable=True), max_failures=3)

    for _ in range(3):
        selector.record(False)

    assert selector.source.name == "list"
    assert detect.calls == 2


def test_failures_without_a_probe_detect_again():
    detect = Detector("storagegrid", "list")
    selector = SourceSelector(detect, probe=None, max_failures=3)

    for _ in range(3):
        selector.record(False)

    assert detect.calls == 2


def test_the_listing_fallback_retries_detection_after_the_interval():
    clock = Clock()
    detect = Detector("list", "storagegrid")
    selector = SourceSelector(detect, redetect_seconds=3600, clock=clock)
    assert selector.on_fallback

    clock.now = 3599
    selector.record(True)
    assert selector.source.name == "list"

    clock.now = 3600
    selector.record(True)
    assert selector.source.name == "storagegrid"
    assert detect.calls == 2


def test_an_outage_never_leaves_the_exporter_on_the_listing_for_good():
    # The measured scenario, without a probe to tell the outage apart: the
    # failures do detect the listing, and the fallback retry climbs back.
    clock = Clock()
    detect = Detector("storagegrid", "list", "storagegrid")
    selector = SourceSelector(
        detect, max_failures=3, redetect_seconds=3600, clock=clock
    )

    for _ in range(3):
        clock.now += 300
        selector.record(False)
    assert selector.source.name == "list"

    for _ in range(12):
        clock.now += 300
        selector.record(True)
    assert selector.source.name == "storagegrid"


def test_an_explicit_listing_is_not_a_fallback():
    clock = Clock()
    detect = Detector("list")
    selector = SourceSelector(detect, requested="list", clock=clock)

    clock.now = 10 * 3600
    selector.record(True)

    assert not selector.on_fallback
    assert detect.calls == 1


def test_a_zero_interval_disables_the_fallback_retry():
    clock = Clock()
    detect = Detector("list", "storagegrid")
    selector = SourceSelector(detect, redetect_seconds=0, clock=clock)

    clock.now = 10 * 3600
    selector.record(True)

    assert detect.calls == 1


def test_a_detection_that_raises_keeps_the_current_source():
    calls = []

    def detect():
        calls.append(True)
        if len(calls) > 1:
            raise RuntimeError("cannot rebuild the client")
        return Named("storagegrid")

    selector = SourceSelector(detect, probe=None, max_failures=1)
    selector.record(False)

    assert selector.source.name == "storagegrid"
