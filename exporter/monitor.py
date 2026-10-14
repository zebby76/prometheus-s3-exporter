"""One bucket under watch: its credentials, clients, probe and collections.

A `BucketMonitor` holds everything that used to be wired once in `main()`, so
one exporter can hold a hundred of them. It never runs anything on its own
schedule: the credentials refresher calls `refresh_credentials()`, a scheduler
worker calls `collect()`, and the probe runs on its own thread once started.
"""

import logging
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from prometheus_client.core import InfoMetricFamily

from exporter import sources
from exporter.buckets import BucketSpec
from exporter.clients import build_probe_client, build_s3_client
from exporter.credentials import CredentialsUnavailable, ResolvedCredentials
from exporter.endpoints import endpoint_id
from exporter.errors import describe, is_credentials_error
from exporter.handler import probe_state
from exporter.metrics import (
    BucketSizeCollector,
    Snapshot,
    collector_up_gauge,
    gauge_generator,
)
from exporter.probe import ConnectivityProbe
from exporter.selection import SourceSelector
from exporter.sources import SourceContext, UsageSource

# Fraction of the interval beyond which a collection is worth a warning. Past
# this the configured period and the effective one start to diverge, and the
# listing is the only source whose cost grows with the bucket.
SLOW_COLLECTION_RATIO = 0.25

# A collection may use at most a quarter of its bucket's time: the next one is
# due no sooner than ADAPTIVE_FACTOR times the last one's duration. A bucket
# whose listing takes 30 minutes is collected every two hours, with no setting
# to tune, and the small ones keep their interval.
ADAPTIVE_FACTOR = 4

# First collections are spread over this window, not over the whole interval:
# at an hourly interval a bucket would otherwise show no size for up to an hour
# after a restart. Later ones keep their own rhythm (end of the last one plus
# the interval), so the spread holds by itself.
FIRST_COLLECTION_SPREAD_SECONDS = 300

# A Secret holding a wrong key gets every probe rejected; without a floor, a
# hundred such buckets would ask the API for their Secret every 30 seconds.
MIN_REFRESH_GAP_SECONDS = 60


def collect_once(
    source: UsageSource,
    collector: BucketSizeCollector,
    bucket_name,
    interval_seconds=0,
    on_error: Optional[Callable[[Exception], None]] = None,
):
    """Run one collection and publish it. Returns True on success."""
    started = time.perf_counter()
    try:
        usage = source.fetch()
    except Exception as e:
        duration = time.perf_counter() - started
        logging.error("Collect S3 Bucket %s failed: %s", bucket_name, describe(e))
        if on_error is not None:
            on_error(e)
        # Keep the previous figures rather than dropping them to zero: a failed
        # cycle should show up as status 0 and a growing staleness, not as a
        # bucket that suddenly looks empty.
        previous = collector.snapshot
        collector.update(
            Snapshot(
                usage=previous.usage,
                measured_at=previous.measured_at,
                healthy=False,
                duration_seconds=duration,
                source_name=source.name,
            )
        )
        return False

    duration = time.perf_counter() - started
    collector.update(
        Snapshot(
            usage=usage,
            measured_at=datetime.now(timezone.utc),
            healthy=True,
            duration_seconds=duration,
            source_name=source.name,
        )
    )
    logging.info(
        "Collect S3 Bucket %s via %s in %.3fs - {'bytes':%s,'count':%s}",
        bucket_name,
        source.name,
        duration,
        int(usage.size_bytes),
        int(usage.object_count),
    )

    if interval_seconds and duration > SLOW_COLLECTION_RATIO * interval_seconds:
        logging.warning(
            "Collection took %.1fs, %.0f%% of the %.0fs interval. Raise "
            "'interval' in collector.yml, or move to a constant-cost source.",
            duration,
            duration / interval_seconds * 100,
            interval_seconds,
        )

    return True


@dataclass(frozen=True)
class MonitorSettings:
    """The global settings every bucket starts from."""

    interval_seconds: float
    retry_seconds: float
    connectivity_interval: int
    probe_timeout: int
    max_failures: int
    redetect_seconds: float
    source: str
    sources_config: dict
    # Spread first collections and probes over their first interval. Off for a
    # single bucket, which starts at once as it always has.
    spread: bool = False


class BucketMonitor:
    """One bucket: credentials, clients, probe, source detection and figures."""

    def __init__(
        self,
        spec: BucketSpec,
        settings: MonitorSettings,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
        build_client=build_s3_client,
        build_probe=build_probe_client,
    ):
        self.spec = spec
        self.settings = settings
        self._clock = clock
        self._rng = rng
        self._build_client = build_client
        self._build_probe = build_probe

        self.interval_seconds = (
            spec.interval_minutes * 60
            if spec.interval_minutes
            else settings.interval_seconds
        )
        # A retry must never outlast the interval it is meant to shorten.
        self.retry_seconds = min(settings.retry_seconds, self.interval_seconds)
        self.requested_source = spec.source or settings.source
        # Known from the configuration alone, before any Secret is read: the
        # endpoint is checked whether or not the bucket's credentials are.
        self.endpoint = endpoint_id(spec.endpoint_url, spec.region)

        # Taken for every change of state. A collection reads what it needs
        # under it and runs outside it: a 30-minute listing must not hold up a
        # credential rotation.
        self._lock = threading.RLock()
        self.credentials: Optional[ResolvedCredentials] = None
        self.error: Optional[str] = None
        self.view: Optional[BucketSizeCollector] = None
        self.probe: Optional[ConnectivityProbe] = None
        self.selector: Optional[SourceSelector] = None
        self.source_name = "none"
        self._client = None
        self._detections = 0
        self._probe_stop: Optional[threading.Event] = None

        self.effective_interval = self.interval_seconds
        self.next_due: Optional[float] = None
        self.running = False
        # Set when S3 rejected the credential: the refresher reads the Secret
        # again at once instead of at its next pass.
        self.needs_refresh = False
        self.on_refresh_request: Optional[Callable[[], None]] = None
        self._last_refresh: Optional[float] = None
        # Set when a reload removed or replaced this bucket: nothing starts
        # again, and a collection that was running is not published.
        self.stopped = False

    # -- state ----------------------------------------------------------------

    @property
    def configured(self) -> bool:
        return self.view is not None

    @property
    def bucket(self) -> Optional[str]:
        return self.credentials.bucket if self.credentials else None

    def collectable(self) -> bool:
        return (
            not self.stopped
            and self.configured
            and self.spec.collect
            and self.next_due is not None
        )

    # -- credentials ----------------------------------------------------------

    def refresh_credentials(self):
        """Read the credentials again and act on any change. Never raises."""
        if self.stopped:
            return
        self._last_refresh = self._clock()
        try:
            credentials = self.spec.credentials.resolve()
        except CredentialsUnavailable as exc:
            self._unreadable(str(exc))
            return
        except Exception as exc:
            self._unreadable(f"{self.spec.credentials.describe()}: {describe(exc)}")
            return

        with self._lock:
            if self.stopped:
                # Removed by a reload while the Secret was being read.
                return
            self.needs_refresh = False
            if self.credentials is None:
                self._start(credentials)
            elif credentials.bucket != self.credentials.bucket:
                logging.warning(
                    "%s now names bucket %s instead of %s: watching it from scratch",
                    self.spec.key,
                    credentials.bucket,
                    self.credentials.bucket,
                )
                self._stop_probe()
                self.credentials = None
                self.view = None
                self._start(credentials)
            elif credentials != self.credentials:
                self._rotate(credentials)
            elif self.error is not None:
                logging.info("%s is readable again", self.spec.key)
                self.error = None

    def _unreadable(self, message: str):
        with self._lock:
            first = self.error != message
            self.error = message
            self.needs_refresh = False
            known = self.credentials is not None
        if known:
            # A Secret that cannot be read right now -- an API blip -- does not
            # make the credentials already in hand wrong.
            logging.log(
                logging.WARNING if first else logging.DEBUG,
                "Cannot read %s again, keeping its last credentials: %s",
                self.spec.key,
                message,
            )
        else:
            logging.log(
                logging.ERROR if first else logging.DEBUG,
                "Bucket %s is not watched: %s",
                self.spec.key,
                message,
            )

    def _start(self, credentials: ResolvedCredentials):
        try:
            client = self._build_client(
                credentials.access_key,
                credentials.secret_key,
                self.spec.region,
                self.spec.endpoint_url,
            )
        except Exception as exc:
            self.error = f"cannot build the S3 client: {exc}"
            logging.error("Bucket %s is not watched: %s", self.spec.key, self.error)
            return

        self.credentials = credentials
        self._client = client
        self._detections = 0
        self.error = None

        interval = self.settings.connectivity_interval
        probe = None
        if interval:
            probe = ConnectivityProbe(
                self._probe_client(credentials) or client,
                credentials.bucket,
                interval,
                on_error=self._on_s3_error,
                name=f"{self.spec.label_namespace}/{credentials.bucket}",
            )
        self.probe = probe

        view = BucketSizeCollector(
            self.spec.label_namespace,
            credentials.bucket,
            self.effective_interval,
            probe,
            collects=self.spec.collect,
        )
        try:
            # Detects the source at once, over the network.
            self.selector = SourceSelector(
                self._detect,
                requested=self.requested_source,
                probe=probe,
                max_failures=self.settings.max_failures,
                redetect_seconds=self.settings.redetect_seconds,
                clock=self._clock,
            )
        except Exception as exc:
            # Back to unconfigured as a whole: the next refresh starts over.
            self.credentials = None
            self.probe = None
            self._client = None
            self.error = f"cannot detect the usage source: {describe(exc)}"
            logging.error("Bucket %s is not watched: %s", self.spec.key, self.error)
            return
        self.view = view

        spread = self.settings.spread
        if probe is not None:
            self._probe_stop = threading.Event()
            probe.start(self._probe_stop, self._rng() * interval if spread else 0)
        window = min(self.interval_seconds, FIRST_COLLECTION_SPREAD_SECONDS)
        self.next_due = self._clock() + (self._rng() * window if spread else 0)
        logging.info(
            "Watching bucket %s (%s, %s)",
            credentials.bucket,
            self.spec.key,
            "collected" if self.spec.collect else "probe only",
        )

    def _rotate(self, credentials: ResolvedCredentials):
        logging.info("Credentials of %s changed: rebuilding its clients", self.spec.key)
        self.credentials = credentials
        self.error = None
        # Detection builds fresh clients from the new credentials, the probe's
        # included, and may settle on another source for the new key.
        self.selector.redetect("credentials changed")

    def _probe_client(self, credentials: ResolvedCredentials):
        """A client sized for the probe, or None to share the collection one."""
        try:
            return self._build_probe(
                credentials.access_key,
                credentials.secret_key,
                self.spec.region,
                self.spec.endpoint_url,
                self.settings.probe_timeout,
            )
        except Exception as exc:
            logging.error(
                "Cannot build the probe client of %s, sharing the collection one: %s",
                self.spec.key,
                exc,
            )
            return None

    def _detect(self) -> UsageSource:
        """Detect the usage source; from the second time on, with fresh clients."""
        credentials = self.credentials
        client = self._client
        if self._detections:
            # Raises when the client cannot be rebuilt; the selector then keeps
            # the current source.
            client = self._build_client(
                credentials.access_key,
                credentials.secret_key,
                self.spec.region,
                self.spec.endpoint_url,
            )
            self._client = client
        self._detections += 1

        context = SourceContext(
            endpoint_url=self.spec.endpoint_url,
            region=self.spec.region,
            bucket=credentials.bucket,
            access_key=credentials.access_key,
            secret_key=credentials.secret_key,
            client=client,
            config=self.settings.sources_config,
        )
        detected = sources.resolve(context, self.requested_source)
        self.source_name = detected.name

        if self._detections > 1 and self.probe is not None:
            probe_client = self._probe_client(credentials)
            if probe_client is not None:
                self.probe.set_client(probe_client)
        return detected

    def _on_s3_error(self, exc: Exception):
        if not is_credentials_error(exc):
            return
        last = self._last_refresh
        if last is None or self._clock() - last >= MIN_REFRESH_GAP_SECONDS:
            self.needs_refresh = True
            if self.on_refresh_request is not None:
                self.on_refresh_request()

    def _stop_probe(self):
        if self._probe_stop is not None:
            self._probe_stop.set()
            self._probe_stop = None

    def stop(self):
        with self._lock:
            self.stopped = True
            self._stop_probe()

    # -- collection -------------------------------------------------------------

    def collect(self):
        """Run one collection and schedule the next. Called by a worker."""
        with self._lock:
            if not self.configured:
                return
            view = self.view
            source = self.selector.source
            bucket = self.credentials.bucket

        succeeded = collect_once(source, view, bucket, on_error=self._on_s3_error)

        with self._lock:
            if self.stopped or view is not self.view:
                # Removed by a reload, or the Secret was repointed, while this
                # ran: these figures belong to a bucket no longer watched.
                return
            self.selector.record(succeeded)
            if succeeded:
                duration = view.snapshot.duration_seconds
                effective = max(self.interval_seconds, ADAPTIVE_FACTOR * duration)
                if effective != self.effective_interval and (
                    effective > self.interval_seconds
                    or self.effective_interval > self.interval_seconds
                ):
                    logging.info(
                        "Collection of %s took %.1fs: next one in %.0fs",
                        bucket,
                        duration,
                        effective,
                    )
                self.effective_interval = effective
                view.interval_seconds = effective
                delay = effective
            else:
                delay = min(self.retry_seconds, self.effective_interval)
            self.next_due = self._clock() + delay

    # -- what it shows ----------------------------------------------------------

    def rows(self) -> List[list]:
        """Metric rows for this bucket; only `bucket_configured` until it is."""
        with self._lock:
            view = self.view
            rows = [
                [
                    ["webtech_s3", "bucket_configured"],
                    {
                        "bucket_key": self.spec.key,
                        "bucket_namespace": self.spec.label_namespace,
                        "name": self.bucket or "",
                    },
                    int(view is not None),
                    "1 when the bucket's credentials could be read and its "
                    "clients built.",
                ]
            ]
        if view is not None:
            rows += view.rows()
        return rows

    def readiness(self) -> Dict:
        """This bucket's entry in the /readyz answer."""
        with self._lock:
            if self.view is None:
                return {
                    "name": self.bucket,
                    "state": "unconfigured",
                    "error": self.error,
                }
            probe = self.probe
            name = self.bucket
        if probe is None:
            return {"name": name, "state": "unknown"}
        return probe_state(name, probe.result)


class ExporterCollector:
    """Every watched bucket's metrics, one family per metric, plus the exporter's."""

    def __init__(
        self,
        monitors,
        scheduler,
        version: str,
        endpoints=None,
        config=None,
    ):
        # A list, or the Fleet a reload changes: iterated afresh every scrape.
        self.monitors = monitors
        self.scheduler = scheduler
        self.version = version
        # The EndpointRegistry, or None when the probes are off.
        self.endpoints = endpoints
        # The ConfigReloader, for its own metrics; None in tests.
        self.config = config

    def collect(self):
        rows = []
        info = InfoMetricFamily(
            "webtech_s3_exporter",
            "Webtech Prometheus Exporter version",
            labels=["bucket_namespace", "name"],
        )
        succeeded = True
        configured = 0
        # Once, so a reload in the middle of a scrape cannot skew the counts.
        monitors = list(self.monitors)
        for monitor in monitors:
            try:
                rows += monitor.rows()
            except Exception as exc:
                succeeded = False
                logging.error("Metrics of %s: %s", monitor.spec.key, exc)
                continue
            if monitor.configured:
                configured += 1
                info.add_metric(
                    [monitor.spec.label_namespace, monitor.bucket or ""],
                    {
                        "version": self.version,
                        "source": monitor.source_name,
                        # Joins a bucket to its webtech_s3_endpoint_* series.
                        "endpoint": monitor.endpoint or "",
                    },
                )

        if self.config is not None:
            rows += self.config.rows()
        if self.endpoints is not None:
            try:
                rows += self.endpoints.rows()
            except Exception as exc:
                succeeded = False
                logging.error("Metrics of the endpoints: %s", exc)

        for state, count in (
            ("configured", configured),
            ("unconfigured", len(monitors) - configured),
        ):
            rows.append(
                [
                    ["webtech_s3", "buckets"],
                    {"state": state},
                    count,
                    "Buckets in the configuration, by whether they are watched.",
                ]
            )
        rows.append(
            [
                ["webtech_s3", "collections_running"],
                {},
                self.scheduler.running(),
                "Collections in progress.",
            ]
        )
        rows.append(
            [
                ["webtech_s3", "collections_overdue"],
                {},
                self.scheduler.overdue(),
                "Collections past their due time with no worker free: raise "
                "max_concurrent_collections or the CPU limit.",
            ]
        )

        yield from gauge_generator(rows)
        yield info
        yield collector_up_gauge(
            ["webtech_s3", "exporter"],
            "1 when the exporter could render its metrics.",
            succeeded=succeeded,
        )
