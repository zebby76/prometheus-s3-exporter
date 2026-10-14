"""What a bucket looks like to Prometheus: its last snapshot and its gauges."""

import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from prometheus_client.core import GaugeMetricFamily

from exporter.sources import Usage
from exporter.utils import merge_dicts_ordered

metric_invalid_chars = re.compile(r"[^a-zA-Z0-9_:]")
metric_invalid_start_chars = re.compile(r"^[^a-zA-Z_:]")
label_invalid_chars = re.compile(r"[^a-zA-Z0-9_]")
label_invalid_start_chars = re.compile(r"^[^a-zA-Z_]")
label_start_double_under = re.compile(r"^__+")


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
    def __init__(
        self, namespace, bucket, interval_seconds=0, probe=None, collects=True
    ):
        self.namespace = namespace
        self.bucket = bucket
        self.interval_seconds = interval_seconds
        # False for a probe-only bucket: no size, count or collection figures at
        # all, rather than zeros that read as an empty bucket.
        self.collects = collects
        # Written by the probe thread only, read here: two frozen objects with
        # one writer each, so neither needs a lock.
        self.probe = probe
        self._snapshot = Snapshot()

    @property
    def snapshot(self) -> Snapshot:
        return self._snapshot

    def update(self, snapshot: Snapshot):
        self._snapshot = snapshot

    def rows(self):
        """This bucket's metrics, as [name parts, labels, value, help] rows.

        Rows rather than families, so an exporter watching many buckets can
        merge every bucket's rows into one family per metric: a family that
        appears twice is refused by Prometheus. Raises when they cannot be
        computed.
        """
        metrics = []
        collector_metric_name = ["webtech_s3"]

        # Read once: the collection thread may swap it under us.
        snapshot = self.snapshot
        usage = snapshot.usage or Usage(size_bytes=0, object_count=0)

        prometheus_labels = {}

        prometheus_namespace_label = OrderedDict({"bucket_namespace": self.namespace})
        prometheus_name_label = OrderedDict({"name": self.bucket})

        prometheus_labels = merge_dicts_ordered(
            prometheus_labels, prometheus_namespace_label
        )
        prometheus_labels = merge_dicts_ordered(
            prometheus_labels, prometheus_name_label
        )

        if self.collects:
            state_metric_name = collector_metric_name + ["exporter", "status"]
            state_metric_description = (
                "1 when the last collection of the bucket usage succeeded."
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
            # Every reason from the start, at zero: increase() over a series
            # that appears with its first error misses that error.
            for reason, count in self.probe.errors_total.items():
                metrics.append(
                    [
                        collector_metric_name + ["bucket_probe_errors_total"],
                        merge_dicts_ordered(prometheus_labels, reason=reason),
                        float(count),
                        "Connectivity probe attempts that failed, by reason.",
                    ]
                )
            connections = self.probe.connections_total
            if connections is not None:
                metrics.append(
                    [
                        collector_metric_name + ["bucket_probe_connections_total"],
                        prometheus_labels,
                        float(connections),
                        "TCP connections the connectivity probe established "
                        "since start.",
                    ]
                )
            dns = self.probe.dns
            metrics.append(
                [
                    collector_metric_name + ["bucket_dns_duration_seconds"],
                    prometheus_labels,
                    dns.duration_seconds,
                    "Duration of the last resolution of the endpoint name, "
                    "in seconds.",
                ]
            )
            metrics.append(
                [
                    collector_metric_name + ["bucket_dns_failures_total"],
                    prometheus_labels,
                    float(self.probe.dns_failures_total),
                    "Resolutions of the endpoint name that failed since start.",
                ]
            )

        return metrics

    def collect(self):
        up_metric_name = ["webtech_s3", "exporter"]
        up_metric_description = "1 when the exporter could render its metrics."
        try:
            metrics = self.rows()
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
