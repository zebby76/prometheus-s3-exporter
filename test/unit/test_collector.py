"""Snapshot semantics, collection outcomes, and configuration loading."""

import logging
import time
from datetime import datetime, timedelta, timezone

from exporter import (
    DEFAULT_CONNECTIVITY_INTERVAL_SECONDS,
    BucketSizeCollector,
    Snapshot,
    collect_once,
    load_configuration,
)
from exporter.probe import ProbeResult
from exporter.sources.base import SourceUnavailable, Usage


class StubSource:
    name = "stub"

    def __init__(self, usage=None, error=None):
        self._usage = usage
        self._error = error

    def fetch(self):
        if self._error is not None:
            raise self._error
        return self._usage


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def test_stale_seconds_uses_the_provider_timestamp():
    usage = Usage(1, 1, as_of=NOW - timedelta(minutes=10))
    snapshot = Snapshot(usage=usage, measured_at=NOW)
    assert snapshot.stale_seconds(now=NOW) == 600


def test_stale_seconds_falls_back_to_our_own_collection_time():
    # The listing has no provider timestamp: the age is how long ago we walked
    # the bucket ourselves.
    usage = Usage(1, 1, as_of=None)
    snapshot = Snapshot(usage=usage, measured_at=NOW - timedelta(minutes=3))
    assert snapshot.stale_seconds(now=NOW) == 180


def test_stale_seconds_is_zero_before_the_first_collection():
    assert Snapshot().stale_seconds(now=NOW) == 0.0


def test_successful_collection_publishes_the_usage():
    collector = BucketSizeCollector("ns", "my-bucket")
    source = StubSource(usage=Usage(4446391, 5))

    assert collect_once(source, collector, "my-bucket") is True
    assert collector.snapshot.usage.size_bytes == 4446391
    assert collector.snapshot.healthy is True
    assert collector.snapshot.source_name == "stub"


def test_failed_collection_keeps_the_previous_figures():
    collector = BucketSizeCollector("ns", "my-bucket")
    collect_once(StubSource(usage=Usage(4446391, 5)), collector, "my-bucket")

    failing = StubSource(error=SourceUnavailable("boom"))
    assert collect_once(failing, collector, "my-bucket") is False

    # Dropping to zero would read as an emptied bucket; the failure has to show
    # up as status 0 and a growing staleness instead.
    assert collector.snapshot.usage.size_bytes == 4446391
    assert collector.snapshot.healthy is False


def _metric_names(collector):
    return {metric.name for metric in collector.collect()}


def test_collect_emits_both_the_new_and_the_deprecated_size():
    collector = BucketSizeCollector("ns", "my-bucket")
    collect_once(StubSource(usage=Usage(4446391, 5)), collector, "my-bucket")

    names = _metric_names(collector)
    assert "webtech_s3_bucket_size_bytes" in names
    assert "webtech_s3_bucket_size_kbytes" in names
    assert "webtech_s3_usage_stale_seconds" in names
    assert "webtech_s3_collect_duration_seconds" in names


def test_deprecated_gauge_is_the_byte_value_divided_by_a_thousand():
    collector = BucketSizeCollector("ns", "my-bucket")
    collect_once(StubSource(usage=Usage(4446391, 5)), collector, "my-bucket")

    values = {
        metric.name: metric.samples[0].value
        for metric in collector.collect()
        if metric.samples
    }
    assert values["webtech_s3_bucket_size_bytes"] == 4446391
    assert values["webtech_s3_bucket_size_kbytes"] == 4446


def test_collect_survives_an_empty_snapshot():
    # Scraped before the first collection: the exporter must answer, not 500.
    collector = BucketSizeCollector("ns", "my-bucket")
    assert "webtech_s3_exporter_up" in _metric_names(collector)


def test_missing_configuration_file_yields_defaults():
    settings = load_configuration("/nonexistent/collector.yml")
    assert settings["interval"] == 1
    assert settings["source"] == "auto"
    assert settings["sources"] == {}


def test_configuration_entries_are_merged(tmp_path):
    path = tmp_path / "collector.yml"
    path.write_text(
        "kind: PrometheusExporterConfig\n"
        "configuration:\n"
        "  - interval: 15\n"
        "  - source: storagegrid\n"
        "  - sources:\n"
        "      minio:\n"
        "        metrics_url: http://minio:9000/x\n",
        encoding="utf-8",
    )
    settings = load_configuration(str(path))
    assert settings["interval"] == 15
    assert settings["source"] == "storagegrid"
    assert settings["sources"]["minio"]["metrics_url"] == "http://minio:9000/x"


def test_empty_sources_block_is_not_an_error(tmp_path):
    # `sources:` with only comments under it parses as None.
    path = tmp_path / "collector.yml"
    path.write_text("configuration:\n  - sources:\n", encoding="utf-8")
    assert load_configuration(str(path))["sources"] == {}


def test_environment_overrides_the_configured_source(tmp_path, monkeypatch):
    path = tmp_path / "collector.yml"
    path.write_text("configuration:\n  - source: list\n", encoding="utf-8")
    monkeypatch.setenv("COLLECTOR_SOURCE", "storagegrid")
    assert load_configuration(str(path))["source"] == "storagegrid"


def test_configured_interval_is_published():
    # Alerts compare a collection against its own budget, so the budget has to
    # be a metric rather than a number hard-coded in the alerting rules.
    collector = BucketSizeCollector("ns", "my-bucket", interval_seconds=300)
    collect_once(StubSource(usage=Usage(1, 1)), collector, "my-bucket")

    values = {
        metric.name: metric.samples[0].value
        for metric in collector.collect()
        if metric.samples
    }
    assert values["webtech_s3_collect_interval_seconds"] == 300


def test_slow_collection_is_warned_about(caplog):
    collector = BucketSizeCollector("ns", "my-bucket", interval_seconds=60)

    class SlowSource:
        name = "slow"

        def fetch(self):
            # Push the measured duration past 25% of the interval.
            time.sleep(0.05)
            return Usage(1, 1)

    with caplog.at_level(logging.WARNING):
        collect_once(SlowSource(), collector, "my-bucket", interval_seconds=0.1)

    assert any("Collection took" in record.message for record in caplog.records)


def test_fast_collection_is_not_warned_about(caplog):
    collector = BucketSizeCollector("ns", "my-bucket", interval_seconds=60)
    with caplog.at_level(logging.WARNING):
        collect_once(StubSource(usage=Usage(1, 1)), collector, "my-bucket", 60)

    assert not any("Collection took" in record.message for record in caplog.records)


class StubProbe:
    def __init__(self, result, retries_total=0):
        self.result = result
        self.retries_total = retries_total


def test_probe_metrics_are_published_when_a_probe_is_wired():
    probe = StubProbe(
        ProbeResult(reachable=True, duration_seconds=0.012, checked_at=NOW),
        retries_total=7,
    )
    collector = BucketSizeCollector("ns", "my-bucket", 60, probe=probe)
    collect_once(StubSource(usage=Usage(1, 1)), collector, "my-bucket")

    values = {
        metric.name: metric.samples[0].value
        for metric in collector.collect()
        if metric.samples
    }
    assert values["webtech_s3_bucket_reachable"] == 1
    assert values["webtech_s3_bucket_probe_duration_seconds"] == 0.012
    assert "webtech_s3_bucket_probe_age_seconds" in values
    # Published so a reconnection reads as a reconnection instead of hiding in
    # the duration as latency.
    assert values["webtech_s3_bucket_probe_retries_total"] == 7


def test_an_unreachable_bucket_is_reported_as_zero():
    probe = StubProbe(ProbeResult(reachable=False, checked_at=NOW, error="boom"))
    collector = BucketSizeCollector("ns", "my-bucket", 60, probe=probe)

    values = {
        metric.name: metric.samples[0].value
        for metric in collector.collect()
        if metric.samples
    }
    assert values["webtech_s3_bucket_reachable"] == 0


def test_no_probe_metrics_when_the_probe_is_disabled():
    # Disabling has to be a true no-op, not a metric stuck at zero that would
    # read as a permanent outage.
    collector = BucketSizeCollector("ns", "my-bucket", 60, probe=None)
    names = {metric.name for metric in collector.collect()}

    assert not any(name.startswith("webtech_s3_bucket_probe") for name in names)
    assert "webtech_s3_bucket_reachable" not in names


def test_collection_metrics_survive_an_unreachable_bucket():
    # The two signals are independent: figures collected an hour ago stay
    # published even while connectivity is down.
    probe = StubProbe(ProbeResult(reachable=False, checked_at=NOW, error="boom"))
    collector = BucketSizeCollector("ns", "my-bucket", 60, probe=probe)
    collect_once(StubSource(usage=Usage(4446391, 5)), collector, "my-bucket")

    values = {
        metric.name: metric.samples[0].value
        for metric in collector.collect()
        if metric.samples
    }
    assert values["webtech_s3_bucket_size_bytes"] == 4446391
    assert values["webtech_s3_exporter_status"] == 1
    assert values["webtech_s3_bucket_reachable"] == 0


def test_connectivity_and_retry_defaults():
    settings = load_configuration("/nonexistent/collector.yml")
    assert settings["connectivity_interval"] == DEFAULT_CONNECTIVITY_INTERVAL_SECONDS
    assert settings["retry_interval"] == 300


def test_connectivity_interval_can_be_overridden_from_the_environment(monkeypatch):
    monkeypatch.setenv("COLLECTOR_CONNECTIVITY_INTERVAL", "5")
    assert load_configuration("/nonexistent")["connectivity_interval"] == 5


def test_a_bad_connectivity_override_keeps_the_default(monkeypatch):
    monkeypatch.setenv("COLLECTOR_CONNECTIVITY_INTERVAL", "soon")
    assert (
        load_configuration("/nonexistent")["connectivity_interval"]
        == DEFAULT_CONNECTIVITY_INTERVAL_SECONDS
    )


def test_environment_override_applies_without_a_configuration_file(monkeypatch):
    # An early return on FileNotFoundError used to skip the overrides entirely,
    # so COLLECTOR_SOURCE was silently ignored on a deployment with no mounted
    # ConfigMap -- exactly the case the override exists for.
    monkeypatch.setenv("COLLECTOR_SOURCE", "list")
    monkeypatch.setenv("COLLECTOR_CONNECTIVITY_INTERVAL", "5")

    settings = load_configuration("/nonexistent/collector.yml")
    assert settings["source"] == "list"
    assert settings["connectivity_interval"] == 5
