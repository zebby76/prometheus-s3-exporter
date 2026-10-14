import argparse
import logging
import os
import signal
import sys
import threading
from typing import List

import yaml
from prometheus_client.core import REGISTRY

from exporter import sources
from exporter._version import get_versions
from exporter.buckets import (
    BucketSpec,
    ConfigError,
    multi_bucket,
    parse_buckets,
    single_bucket_spec,
)
from exporter.clients import (  # noqa: F401 -- re-exported
    build_probe_client,
    build_s3_client,
    probe_client_for,
    probe_timeout_for,
)
from exporter.credentials import CredentialsUnavailable
from exporter.endpoints import EndpointRegistry
from exporter.handler import (
    DEFAULT_PROXY_HEADERS,
    exporter,
    health,
    set_proxy_headers,
)
from exporter.kube import InClusterKubernetes
from exporter.metrics import (  # noqa: F401 -- re-exported
    BucketSizeCollector,
    Snapshot,
    collector_up_gauge,
    format_label_key,
    format_label_value,
    format_metric_name,
    gauge_generator,
    group_metrics,
)
from exporter.monitor import (  # noqa: F401 -- collect_once re-exported
    BucketMonitor,
    ExporterCollector,
    MonitorSettings,
    collect_once,
)
from exporter.scheduler import CollectionScheduler, CredentialsRefresher
from exporter.selection import DEFAULT_REDETECT_SECONDS

__version__ = get_versions()["version"]


CONFIG_PATH = os.getenv("COLLECTOR_CONFIG", "config/collector.yml")

# Minutes between two collections. A bucket size is a capacity figure: few
# dashboards need it fresher, and the listing source pays for every cycle.
DEFAULT_INTERVAL_MINUTES = 5
# With a bucket list, collections are background work: the probe answers
# reachability every 30 seconds, and a size an hour old is fresh enough.
DEFAULT_MULTI_BUCKET_INTERVAL_MINUTES = 60
# Collections running at once. Each worker can hold a listing for a long time;
# the probes run apart and never wait for one.
DEFAULT_MAX_CONCURRENT_COLLECTIONS = 2
# Seconds between two reads of every bucket's Secret: how soon a rotated key or
# a newly granted RoleBinding is picked up.
DEFAULT_CREDENTIALS_REFRESH_SECONDS = 300
# Consecutive fetch failures after which the source is re-detected. Without
# this, a provider that is briefly unreachable at startup would pin the
# exporter to the listing fallback until someone restarts the pod.
DEFAULT_MAX_FAILURES = 3


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
        "redetect_interval": DEFAULT_REDETECT_SECONDS,
        "probe_timeout": DEFAULT_PROBE_TIMEOUT_SECONDS,
        "proxy_headers": dict(DEFAULT_PROXY_HEADERS),
        "max_concurrent_collections": DEFAULT_MAX_CONCURRENT_COLLECTIONS,
        "credentials_refresh": DEFAULT_CREDENTIALS_REFRESH_SECONDS,
    }
    configured = set()

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
            configured.update(entry)

    # The default depends on the mode; an interval written down always wins.
    if "interval" not in configured and multi_bucket(settings):
        settings["interval"] = DEFAULT_MULTI_BUCKET_INTERVAL_MINUTES

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


def positive_number(settings, key, unit):
    """Read a setting that must be a number above zero, or stop the exporter.

    An `interval` of 0 used to mean no wait at all between two collections:
    measured, about 200 collections a second against the storage. A setting
    that cannot be honoured stops the start, loudly, rather than being guessed.
    """
    value = settings.get(key)
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if not number > 0:
        logging.error(
            "Configuration '%s' must be a number of %s above 0, got %r",
            key,
            unit,
            value,
        )
        sys.exit(1)
    return number


def require_env(name):
    value = os.getenv(name, "")
    if not value:
        # Exit non-zero: a zero status makes a misconfigured container look like
        # a clean shutdown, so it never lands in CrashLoopBackOff and no one
        # gets alerted.
        logging.error("Environment variable %s cannot be null or undefined !", name)
        sys.exit(1)
    return value


def bucket_specs(settings, environ=None, kube=None) -> List[BucketSpec]:
    """The buckets to watch: the configured list, or the one the environment names.

    Raises ConfigError when the list cannot be honoured.
    """
    environ = os.environ if environ is None else environ
    if not multi_bucket(settings):
        return [single_bucket_spec(environ)]
    if kube is None:
        kube = InClusterKubernetes.detect(environ)
    return parse_buckets(settings, environ, kube)


def check_config(specs: List[BucketSpec], out=None) -> int:
    """Resolve every bucket's credentials once and report; 0 when all resolve.

    What an operator runs (`oc exec … exporter --check-config`) to find the
    Secret, key or RoleBinding that is missing before going live. Reports
    names only: no value read from a Secret is ever printed.
    """
    out = out or sys.stdout
    failures = 0
    for spec in specs:
        try:
            credentials = spec.credentials.resolve()
        except CredentialsUnavailable as exc:
            failures += 1
            print(f"FAIL  {exc}", file=out)
            continue
        print(
            f"OK    {spec.credentials.describe()}: bucket {credentials.bucket}, "
            f"namespace {spec.label_namespace}, region {spec.region}, "
            f"endpoint {spec.endpoint_url or 'AWS default'}",
            file=out,
        )
    print(
        f"{len(specs) - failures} of {len(specs)} bucket(s) resolved",
        file=out,
    )
    return 1 if failures else 0


def main():

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    parser = argparse.ArgumentParser(
        description="Export the size and object count of an S3 bucket to Prometheus."
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
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate the configuration, resolve every bucket's credentials, "
        "report, and exit: 0 when every bucket resolves, 1 otherwise.",
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

    settings = load_configuration()
    multi = multi_bucket(settings)

    try:
        specs = bucket_specs(settings)
    except ConfigError as e:
        logging.error("Invalid configuration: %s", e)
        sys.exit(1)
    if args.check_config:
        sys.exit(check_config(specs))

    if not multi:
        # The single-bucket mode's own checks, with the messages it always had.
        require_env("OPENSHIFT_NAMESPACE")
        require_env("AWS_ACCESS_KEY_ID")
        require_env("AWS_SECRET_ACCESS_KEY")
        require_env("AWS_DEFAULT_REGION")
        require_env("S3_BUCKET_NAME")

    interval = positive_number(settings, "interval", "minutes")
    connectivity_interval = int(settings["connectivity_interval"])
    monitor_settings = MonitorSettings(
        interval_seconds=interval * 60,
        retry_seconds=positive_number(settings, "retry_interval", "seconds"),
        connectivity_interval=connectivity_interval,
        probe_timeout=probe_timeout_for(
            settings["probe_timeout"], connectivity_interval
        ),
        max_failures=int(settings["max_failures"]),
        redetect_seconds=int(settings["redetect_interval"]),
        source=settings["source"],
        sources_config=settings["sources"],
        spread=len(specs) > 1,
    )
    max_workers = int(
        positive_number(settings, "max_concurrent_collections", "collections")
    )
    refresh_seconds = positive_number(settings, "credentials_refresh", "seconds")

    monitors = [BucketMonitor(spec, monitor_settings) for spec in specs]
    refresher = CredentialsRefresher(monitors, refresh_seconds, STOP)
    for monitor in monitors:
        monitor.on_refresh_request = refresher.request

    # A first pass before anything else runs, so every bucket whose Secret is
    # readable starts at once.
    refresher.refresh_all()
    if not multi and not monitors[0].configured:
        # One bucket and nothing to watch: as before 6.0.0, that stops the start.
        logging.error("Cannot watch the bucket: %s", monitors[0].error)
        sys.exit(1)
    logging.info(
        "Watching %s of %s bucket(s)",
        sum(monitor.configured for monitor in monitors),
        len(monitors),
    )

    endpoints = None
    if connectivity_interval:
        # From the configuration alone: the endpoints are checked even while no
        # Secret is readable yet.
        endpoints = EndpointRegistry(
            connectivity_interval,
            monitor_settings.probe_timeout,
            spread=len(specs) > 1,
        )
        endpoints.sync(monitor.endpoint for monitor in monitors)

    scheduler = CollectionScheduler(monitors, max_workers, STOP)
    refresher.on_change = scheduler.wake
    REGISTRY.register(ExporterCollector(monitors, scheduler, __version__, endpoints))

    set_proxy_headers(settings["proxy_headers"])
    exporter(port=9773)
    health(
        port=9774,
        readiness=(
            (lambda: {monitor.spec.key: monitor.readiness() for monitor in monitors})
            if connectivity_interval
            else None
        ),
        endpoints=endpoints.readiness if endpoints is not None else None,
    )

    refresher.start()
    scheduler.start()

    # Woken by SIGTERM/SIGINT; the timeout keeps the main thread responsive to
    # signals on every platform.
    while not STOP.wait(1):
        pass

    scheduler.shutdown()
    if endpoints is not None:
        endpoints.stop()
    for monitor in monitors:
        monitor.stop()
    shutdown()
