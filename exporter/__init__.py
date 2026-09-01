import argparse
import logging
import os
import re
import signal
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import boto3
import yaml
from botocore.config import Config
from prometheus_client import Info
from prometheus_client.core import REGISTRY, GaugeMetricFamily

from exporter import sources
from exporter._version import get_versions
from exporter.handler import (
    DEFAULT_PROXY_HEADERS,
    exporter,
    health,
    set_proxy_headers,
)
from exporter.probe import ConnectivityProbe
from exporter.sources import SourceContext, Usage, UsageSource

from .utils import merge_dicts_ordered

__version__ = get_versions()["version"]

gauges = {}

metric_invalid_chars = re.compile(r"[^a-zA-Z0-9_:]")
metric_invalid_start_chars = re.compile(r"^[^a-zA-Z_:]")
label_invalid_chars = re.compile(r"[^a-zA-Z0-9_]")
label_invalid_start_chars = re.compile(r"^[^a-zA-Z_]")
label_start_double_under = re.compile(r"^__+")

CONFIG_PATH = os.getenv("COLLECTOR_CONFIG", "config/collector.yml")

DEFAULT_INTERVAL_MINUTES = 1
# Consecutive fetch failures after which the source is re-detected. Without
# this, a provider that is briefly unreachable at startup would pin the
# exporter to the listing fallback until someone restarts the pod.
DEFAULT_MAX_FAILURES = 3

# Fraction of the interval beyond which a collection is worth a warning. Past
# this the configured period and the effective one start to diverge, and the
# listing is the only source whose cost grows with the bucket.
SLOW_COLLECTION_RATIO = 0.25

# Seconds between reachability probes. The collection interval is sized for the
# cost of measuring the bucket, which on a large one runs into tens of minutes;
# reachability has to be answered far more often than that. 0 disables it.
#
# 30 and not 60: between two hourly collections the probe is the only traffic on
# its connection, so the interval is also what keeps that connection alive. A 60
# second probe against the equally common 60 second idle timeout sits exactly on
# the boundary and reconnects on roughly every other cycle -- measured, and the
# reason this default moved. Keep it under the idle timeout in front of the
# storage system.
DEFAULT_CONNECTIVITY_INTERVAL_SECONDS = 30

# Seconds allowed for one connectivity probe, for connect and for read alike.
# Clamped against the probe interval so two timeouts always fit inside one
# cycle: a probe must never still be running when the next one is due.
DEFAULT_PROBE_TIMEOUT_SECONDS = 5

# Seconds to wait before retrying a failed collection, capped at the interval.
# At a 60 minute interval a single transient failure would otherwise leave the
# figures stale for a full hour.
DEFAULT_RETRY_INTERVAL_SECONDS = 300

# Signalled by the SIGTERM/SIGINT handlers; also cuts the sleep between cycles
# short, so a pod stops within moments instead of after a whole interval.
STOP = threading.Event()


def shutdown():
    logging.info("Shutting down")
    sys.exit(0)


def signal_handler(signum, _frame):
    # signal.signal always calls the handler with (signum, frame); a zero-arg
    # handler raises TypeError and turns every SIGTERM into a crash.
    logging.info("Received signal %s", signum)
    STOP.set()


def build_s3_client(
    aws_access_key_id,
    aws_secret_access_key,
    aws_default_region,
    aws_endpoint_url,
    config=None,
):
    """Build the S3 client. Raises so the caller can decide what to do."""
    return boto3.client(
        service_name="s3",
        region_name=aws_default_region or None,
        endpoint_url=aws_endpoint_url or None,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        config=config,
    )


def probe_timeout_for(configured, interval_seconds):
    """A whole check has to fit inside the interval it serves.

    Two attempts of connect plus read, so four timeouts: a probe must never
    still be running when the next one is due.
    """
    if not interval_seconds:
        return int(configured)
    return max(1, min(int(configured), interval_seconds // 4))


def build_probe_client(
    aws_access_key_id,
    aws_secret_access_key,
    aws_default_region,
    aws_endpoint_url,
    timeout_seconds,
):
    """A client sized for one round trip, not for a long listing.

    The collection client keeps the boto3 defaults, which are right for it: a
    transient error in the middle of 150 pages should be retried, not fail the
    hour. A probe wants the opposite.
    """
    return build_s3_client(
        aws_access_key_id,
        aws_secret_access_key,
        aws_default_region,
        aws_endpoint_url,
        config=Config(
            connect_timeout=timeout_seconds,
            read_timeout=timeout_seconds,
            # No botocore retry at all. Zero and not one, because botocore
            # counts retries here rather than attempts, so max_attempts=1 would
            # still permit a second request; this resolves to
            # total_max_attempts=1.
            #
            # The reason is the backoff, not the extra request. Both retry
            # modes sleep rand(0, 1) seconds before the first retry -- legacy
            # by way of `"base": "rand"` in _retry.json, and standard the same,
            # since the cheaper 0.05 scaling in its ExponentialBackoff sits
            # behind NEW_RETRIES_ENABLED, an internal flag that is off. That
            # sleep lands inside probe_duration_seconds and is indistinguishable
            # from real latency. ConnectivityProbe retries once by itself
            # instead: no sleep, and counted.
            retries={"max_attempts": 0, "mode": "standard"},
            # SO_KEEPALIVE on the socket, and nothing more: Linux waits
            # tcp_keepalive_time -- 7200s out of the box -- before the first
            # probe packet, so at any sane probe cadence this fires never. It is
            # set so that lowering that sysctl below the load balancer's idle
            # timeout takes effect. What keeps the connection warm today is the
            # probe traffic itself, which is why the interval has to stay under
            # that timeout -- see DEFAULT_CONNECTIVITY_INTERVAL_SECONDS.
            tcp_keepalive=True,
            max_pool_connections=1,
        ),
    )


def probe_client_for(
    shared_client,
    interval_seconds,
    timeout_seconds,
    aws_access_key_id,
    aws_secret_access_key,
    aws_default_region,
    aws_endpoint_url,
):
    """The probe's own client, or the shared one when there is no probe.

    The probe is auxiliary: a client it cannot build must not stop the exporter
    from starting, unlike the collection client whose failure is fatal.
    """
    if not interval_seconds:
        return shared_client

    try:
        client = build_probe_client(
            aws_access_key_id,
            aws_secret_access_key,
            aws_default_region,
            aws_endpoint_url,
            timeout_seconds,
        )
    except Exception as e:
        logging.error("Cannot build the probe client, using the shared one: %s", e)
        return shared_client

    logging.info(
        "Connectivity probe client: %ss timeout, single attempt, keepalive on",
        timeout_seconds,
    )
    return client


def load_configuration(path=CONFIG_PATH):
    """Read collector.yml, falling back to defaults when it is absent.

    The file is optional on purpose: the production image ships one, but a
    deployment that forgets to mount it should still start rather than die on a
    FileNotFoundError.
    """
    settings = {
        "interval": DEFAULT_INTERVAL_MINUTES,
        "source": sources.AUTO,
        "sources": {},
        "max_failures": DEFAULT_MAX_FAILURES,
        "connectivity_interval": DEFAULT_CONNECTIVITY_INTERVAL_SECONDS,
        "retry_interval": DEFAULT_RETRY_INTERVAL_SECONDS,
        "probe_timeout": DEFAULT_PROBE_TIMEOUT_SECONDS,
        "proxy_headers": dict(DEFAULT_PROXY_HEADERS),
    }

    document = {}
    try:
        with open(path, "r", encoding="utf-8") as ymlfile:
            document = yaml.load(ymlfile, Loader=yaml.SafeLoader) or {}
    except FileNotFoundError:
        logging.info("No configuration at %s, using defaults", path)
    except (OSError, yaml.YAMLError) as e:
        logging.error("Cannot read configuration %s: %s", path, e)

    # `configuration` is a list of mappings; merging them keeps the historical
    # one-key-per-entry layout working alongside a single grouped mapping.
    for entry in document.get("configuration") or []:
        if isinstance(entry, dict):
            settings.update(entry)

    env_source = os.getenv("COLLECTOR_SOURCE")
    if env_source:
        settings["source"] = env_source

    env_probe = os.getenv("COLLECTOR_CONNECTIVITY_INTERVAL")
    if env_probe:
        try:
            settings["connectivity_interval"] = int(env_probe)
        except ValueError:
            logging.error(
                "COLLECTOR_CONNECTIVITY_INTERVAL is not a number: %r", env_probe
            )

    # One variable per field, so a deployment can name a single header without
    # having to restate the other three.
    for field in DEFAULT_PROXY_HEADERS:
        env_header = os.getenv(f"COLLECTOR_PROXY_HEADER_{field.upper()}")
        if env_header:
            headers = settings.get("proxy_headers")
            if not isinstance(headers, dict):
                headers = {}
            settings["proxy_headers"] = {**headers, field: env_header}

    configured_sources = settings.get("sources")
    if configured_sources is None:
        # `sources:` with nothing but comments under it parses as None.
        settings["sources"] = {}
    elif not isinstance(configured_sources, dict):
        logging.error("Configuration key 'sources' must be a mapping, ignoring it")
        settings["sources"] = {}

    return settings


def format_label_key(label_key):
    label_key = re.sub(label_invalid_chars, "_", label_key)
    label_key = re.sub(label_invalid_start_chars, "_", label_key)
    label_key = re.sub(label_start_double_under, "_", label_key)
    return label_key


def format_label_value(value_list):
    if isinstance(value_list, list):
        value_list = [str(v) for v in value_list]
        return "_".join(value_list)
    else:
        return str(value_list)


def format_metric_name(name_list):
    metric = "_".join(name_list)
    metric = re.sub(metric_invalid_chars, "_", metric)
    metric = re.sub(metric_invalid_start_chars, "_", metric)
    return metric.lower()


def group_metrics(metrics):

    metric_dict = {}
    for name_list, label_dict, value, description in metrics:
        metric_name = format_metric_name(name_list)
        label_dict = OrderedDict(
            [
                (format_label_key(k), format_label_value(v))
                for k, v in label_dict.items()
            ]
        )

        if metric_name not in metric_dict:
            metric_dict[metric_name] = (
                tuple(label_dict.keys()),
                {},
                {"description": description},
            )

        label_keys = metric_dict[metric_name][0]
        label_values = tuple([label_dict[key] for key in label_keys])

        metric_dict[metric_name][1][label_values] = value

    logging.debug("Function [group_metrics] - return : %s", metric_dict)
    return metric_dict


def gauge_generator(metrics):
    metric_dict = group_metrics(metrics)

    for metric_name, (label_keys, value_dict, description) in metric_dict.items():
        if label_keys:
            gauge = GaugeMetricFamily(
                metric_name, description["description"], labels=label_keys
            )

            for label_values, value in value_dict.items():
                gauge.add_metric(label_values, value)
        else:
            gauge = GaugeMetricFamily(
                metric_name,
                description["description"],
                value=list(value_dict.values())[0],
            )

        yield gauge


def collector_up_gauge(name_list, description, succeeded=True):
    metric_name = format_metric_name(name_list + ["up"])
    return GaugeMetricFamily(metric_name, description, value=int(succeeded))


@dataclass(frozen=True)
class Snapshot:
    """One consistent view of the bucket, published in a single assignment.

    Size and count used to live in separate module globals, so a scrape landing
    between two writes could report a size from one cycle and a count from the
    next. Replacing the whole snapshot at once removes that window.
    """

    usage: Optional[Usage] = None
    measured_at: Optional[datetime] = None
    healthy: bool = False
    duration_seconds: float = 0.0
    source_name: str = "none"

    def stale_seconds(self, now: Optional[datetime] = None) -> float:
        """Age of the figures currently being served.

        Sources that run a background scanner tell us when they computed the
        numbers; for the others the age is how long ago we collected them. Both
        keep growing while collection is failing, which is the point.
        """
        now = now or datetime.now(timezone.utc)
        if self.usage is not None and self.usage.as_of is not None:
            return max(0.0, (now - self.usage.as_of).total_seconds())
        if self.measured_at is not None:
            return max(0.0, (now - self.measured_at).total_seconds())
        return 0.0


class BucketSizeCollector(object):
    def __init__(self, namespace, bucket, interval_seconds=0, probe=None):
        self.namespace = namespace
        self.bucket = bucket
        self.interval_seconds = interval_seconds
        # Written by the probe thread only, read here: two frozen objects with
        # one writer each, so neither needs a lock.
        self.probe = probe
        self._snapshot = Snapshot()

    @property
    def snapshot(self) -> Snapshot:
        return self._snapshot

    def update(self, snapshot: Snapshot):
        self._snapshot = snapshot

    def collect(self):

        metrics = []

        # Assigned before the try so the except branch can report the failure
        # instead of raising NameError inside the error handler.
        collector_metric_name = ["webtech_s3"]
        up_metric_name = collector_metric_name + ["exporter"]
        up_metric_description = "Did the 'Webtech S3' Prometheus Exporter Up & Running."

        try:
            # Read once: the collection thread may swap it under us.
            snapshot = self.snapshot
            usage = snapshot.usage or Usage(size_bytes=0, object_count=0)

            prometheus_labels = {}

            prometheus_namespace_label = OrderedDict({"namespace": self.namespace})
            prometheus_name_label = OrderedDict({"name": self.bucket})

            prometheus_labels = merge_dicts_ordered(
                prometheus_labels, prometheus_namespace_label
            )
            prometheus_labels = merge_dicts_ordered(
                prometheus_labels, prometheus_name_label
            )

            state_metric_name = collector_metric_name + ["exporter", "status"]
            state_metric_description = (
                "Did the 'Webtech S3' specific Openshift Resource push succeed."
            )

            metrics.append(
                [
                    state_metric_name,
                    prometheus_labels,
                    int(snapshot.healthy),
                    state_metric_description,
                ]
            )

            size_metric_name = collector_metric_name + ["bucket_size_bytes"]
            size_metric_description = "Size of S3 bucket in bytes."

            metrics.append(
                [
                    size_metric_name,
                    prometheus_labels,
                    int(usage.size_bytes),
                    size_metric_description,
                ]
            )

            # Kept for one release so existing dashboards and alerts keep
            # working; drop it at the next major together with this comment.
            legacy_size_metric_name = collector_metric_name + ["bucket_size_kbytes"]
            legacy_size_metric_description = (
                "DEPRECATED, use webtech_s3_bucket_size_bytes. "
                "Size of S3 bucket in kbytes."
            )

            metrics.append(
                [
                    legacy_size_metric_name,
                    prometheus_labels,
                    int(usage.size_bytes / 1000),
                    legacy_size_metric_description,
                ]
            )

            count_metric_name = collector_metric_name + ["bucket_count_total"]
            count_metric_description = "Object Count of S3 bucket."

            metrics.append(
                [
                    count_metric_name,
                    prometheus_labels,
                    int(usage.object_count),
                    count_metric_description,
                ]
            )

            stale_metric_name = collector_metric_name + ["usage_stale_seconds"]
            stale_metric_description = (
                "Age of the usage figures currently exposed, in seconds."
            )

            metrics.append(
                [
                    stale_metric_name,
                    prometheus_labels,
                    snapshot.stale_seconds(),
                    stale_metric_description,
                ]
            )

            duration_metric_name = collector_metric_name + ["collect_duration_seconds"]
            duration_metric_description = "Duration of the last collection, in seconds."

            metrics.append(
                [
                    duration_metric_name,
                    prometheus_labels,
                    snapshot.duration_seconds,
                    duration_metric_description,
                ]
            )

            # Published so alerts can compare a collection against its own
            # budget instead of against a hard-coded number of seconds, which
            # would need retuning for every bucket and interval.
            interval_metric_name = collector_metric_name + ["collect_interval_seconds"]
            interval_metric_description = "Configured interval between collections."

            metrics.append(
                [
                    interval_metric_name,
                    prometheus_labels,
                    float(self.interval_seconds),
                    interval_metric_description,
                ]
            )

            if self.probe is not None:
                probe_result = self.probe.result

                metrics.append(
                    [
                        collector_metric_name + ["bucket_reachable"],
                        prometheus_labels,
                        int(probe_result.reachable),
                        "Whether the last connectivity probe reached the bucket.",
                    ]
                )
                metrics.append(
                    [
                        collector_metric_name + ["bucket_probe_duration_seconds"],
                        prometheus_labels,
                        probe_result.duration_seconds,
                        "Duration of the last connectivity probe, in seconds.",
                    ]
                )
                # Grows without bound if the probe thread dies, which would
                # otherwise leave `bucket_reachable` frozen and trusted.
                metrics.append(
                    [
                        collector_metric_name + ["bucket_probe_age_seconds"],
                        prometheus_labels,
                        probe_result.age_seconds(),
                        "Age of the last connectivity probe, in seconds.",
                    ]
                )
                # Monotonic, so increase() over it reads as a rate even though
                # the collector can only emit gauges. This is what makes a
                # reconnection visible instead of it hiding inside
                # bucket_probe_duration_seconds as latency.
                metrics.append(
                    [
                        collector_metric_name + ["bucket_probe_retries_total"],
                        prometheus_labels,
                        float(self.probe.retries_total),
                        "Connectivity probe attempts retried since start.",
                    ]
                )

        except Exception as e:
            logging.error(e)
            yield collector_up_gauge(
                up_metric_name, up_metric_description, succeeded=False
            )
        else:
            yield from gauge_generator(metrics)
            yield collector_up_gauge(
                up_metric_name, up_metric_description, succeeded=True
            )


def collect_once(
    source: UsageSource,
    collector: BucketSizeCollector,
    bucket_name,
    interval_seconds=0,
):
    """Run one collection and publish it. Returns True on success."""
    started = time.perf_counter()
    try:
        usage = source.fetch()
    except Exception as e:
        duration = time.perf_counter() - started
        logging.error("Collect S3 Bucket %s failed: %s", bucket_name, e)
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


def require_env(name):
    value = os.getenv(name, "")
    if not value:
        # Exit non-zero: a zero status makes a misconfigured container look like
        # a clean shutdown, so it never lands in CrashLoopBackOff and no one
        # gets alerted.
        logging.error("Environment variable %s cannot be null or undefined !", name)
        sys.exit(1)
    return value


def main():

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    parser = argparse.ArgumentParser(
        description="Export Openshift Object Definitions metrics to Prometheus."
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="detail level to log. (default: INFO)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="turn on verbose (DEBUG) logging. Overrides --log-level.",
    )
    args = parser.parse_args()

    log_handler = logging.StreamHandler()
    log_format = "[%(asctime)s] %(name)s.%(levelname)s %(threadName)s %(message)s"
    formatter = logging.Formatter(log_format)
    log_handler.setFormatter(formatter)

    log_level = getattr(logging, args.log_level)
    logging.basicConfig(
        handlers=[log_handler], level=logging.DEBUG if args.verbose else log_level
    )
    logging.captureWarnings(True)

    openshift_namespace = require_env("OPENSHIFT_NAMESPACE")
    aws_access_key_id = require_env("AWS_ACCESS_KEY_ID")
    aws_secret_access_key = require_env("AWS_SECRET_ACCESS_KEY")
    aws_default_region = require_env("AWS_DEFAULT_REGION")
    s3_bucket_name = require_env("S3_BUCKET_NAME")
    # Optional: empty means the real AWS endpoints, which is what the
    # CloudWatch source expects.
    aws_endpoint_url = os.getenv("AWS_ENDPOINT_URL", "")

    settings = load_configuration()
    interval = settings["interval"]
    max_failures = int(settings["max_failures"])
    connectivity_interval = int(settings["connectivity_interval"])
    # A retry must never outlast the interval it is meant to shorten.
    retry_interval = min(int(settings["retry_interval"]), interval * 60)
    probe_timeout = probe_timeout_for(settings["probe_timeout"], connectivity_interval)

    try:
        s3_client = build_s3_client(
            aws_access_key_id,
            aws_secret_access_key,
            aws_default_region,
            aws_endpoint_url,
        )
    except Exception as e:
        logging.error("Cannot build the S3 client: %s", e)
        sys.exit(1)

    context = SourceContext(
        endpoint_url=aws_endpoint_url,
        region=aws_default_region,
        bucket=s3_bucket_name,
        access_key=aws_access_key_id,
        secret_key=aws_secret_access_key,
        client=s3_client,
        config=settings["sources"],
    )

    source = sources.resolve(context, settings["source"])

    # Its own client, so the probe's timeout budget never becomes the
    # collection's.
    probe_client = probe_client_for(
        s3_client,
        connectivity_interval,
        probe_timeout,
        aws_access_key_id,
        aws_secret_access_key,
        aws_default_region,
        aws_endpoint_url,
    )
    probe = ConnectivityProbe(probe_client, s3_bucket_name, connectivity_interval)

    info = Info("webtech_s3_exporter", "Webtech Prometheus Exporter version")

    def publish_info(active_source):
        info.info(
            {
                "version": __version__,
                "namespace": openshift_namespace,
                "name": s3_bucket_name,
                "source": active_source.name,
            }
        )

    publish_info(source)

    collector = BucketSizeCollector(
        namespace=openshift_namespace,
        bucket=s3_bucket_name,
        interval_seconds=interval * 60,
        probe=probe if connectivity_interval else None,
    )
    REGISTRY.register(collector)

    probe.start(STOP)

    set_proxy_headers(settings["proxy_headers"])
    exporter(port=9773)
    health(port=9774, probe=probe if connectivity_interval else None)

    failures = 0
    while not STOP.is_set():
        succeeded = collect_once(source, collector, s3_bucket_name, interval * 60)
        if succeeded:
            failures = 0
        else:
            failures += 1
            if failures >= max_failures:
                logging.warning(
                    "%s consecutive failures, re-detecting the usage source",
                    failures,
                )
                failures = 0
                try:
                    context = SourceContext(
                        endpoint_url=aws_endpoint_url,
                        region=aws_default_region,
                        bucket=s3_bucket_name,
                        access_key=aws_access_key_id,
                        secret_key=aws_secret_access_key,
                        client=build_s3_client(
                            aws_access_key_id,
                            aws_secret_access_key,
                            aws_default_region,
                            aws_endpoint_url,
                        ),
                        config=settings["sources"],
                    )
                except Exception as e:
                    logging.error("Cannot rebuild the S3 client: %s", e)
                else:
                    source = sources.resolve(context, settings["source"])
                    publish_info(source)
                    if connectivity_interval:
                        # Its own try: the source has just been re-resolved
                        # successfully, and a probe client that fails to build
                        # must not undo that.
                        try:
                            probe.set_client(
                                build_probe_client(
                                    aws_access_key_id,
                                    aws_secret_access_key,
                                    aws_default_region,
                                    aws_endpoint_url,
                                    probe_timeout,
                                )
                            )
                        except Exception as e:
                            logging.error(
                                "Cannot rebuild the probe client, keeping the "
                                "current one: %s",
                                e,
                            )

        # Retry sooner than the full interval after a failure: at 60 minutes,
        # one transient error would otherwise mean an hour of stale figures.
        # The wait is interruptible, so SIGTERM is not held for an interval.
        STOP.wait(interval * 60 if succeeded else retry_interval)

    shutdown()
