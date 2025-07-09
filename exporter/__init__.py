import argparse
import logging
import os
import re
import signal
import sys
import threading
import time
from collections import OrderedDict

import boto3
import yaml
from exporter._version import get_versions
from exporter.handler import exporter, health
from prometheus_client import Info
from prometheus_client.core import REGISTRY, GaugeMetricFamily

from .utils import merge_dicts_ordered

__version__ = get_versions()["version"]

gauges = {}

metric_invalid_chars = re.compile(r"[^a-zA-Z0-9_:]")
metric_invalid_start_chars = re.compile(r"^[^a-zA-Z_:]")
label_invalid_chars = re.compile(r"[^a-zA-Z0-9_]")
label_invalid_start_chars = re.compile(r"^[^a-zA-Z_]")
label_start_double_under = re.compile(r"^__+")

COLLECTOR_STATE = None
SIZE_KBYTE = 0
COUNT_TOTAL = 0


def shutdown():
    logging.info("Shutting down")
    sys.exit(1)


def signal_handler():
    shutdown()


def getClient(
    aws_access_key_id,
    aws_secret_access_key,
    aws_default_region,
    aws_endpoint_url,
    s3_bucket_name,
):

    global COLLECTOR_STATE

    try:

        s3 = boto3.resource(
            service_name="s3",
            region_name=aws_default_region,
            endpoint_url=aws_endpoint_url,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
        )

        bucket = s3.Bucket(s3_bucket_name)

    except Exception as e:
        logging.error(e)
        COLLECTOR_STATE = False
    else:
        COLLECTOR_STATE = True
        return bucket


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


def collect(s3bucket, s3_bucket_name):

    global SIZE_KBYTE
    global COUNT_TOTAL
    global COLLECTOR_STATE

    try:

        bucket_size_byte = 0
        bucket_count_total = 0

        for b in s3bucket.objects.all():

            logging.debug(b)
            bucket_size_byte = bucket_size_byte + b.size
            bucket_count_total = bucket_count_total + 1

            logging.debug(bucket_size_byte)
            logging.debug(bucket_count_total)

    except Exception as e:
        COLLECTOR_STATE = False
        logging.error(e)
    else:
        SIZE_KBYTE = bucket_size_byte / 1000
        COUNT_TOTAL = bucket_count_total
        COLLECTOR_STATE = True
        logging.info(
            "Collect S3 Bucket %s - {'size':%s,'count':%s}",
            s3_bucket_name,
            int(SIZE_KBYTE),
            int(COUNT_TOTAL),
        )


class BucketSizeCollector(object):
    def __init__(self, namespace, bucket):
        self.namespace = namespace
        self.bucket = bucket

    def collect(self):

        metrics = []

        try:

            prometheus_labels = {}

            prometheus_namespace_label = OrderedDict({"namespace": self.namespace})
            prometheus_name_label = OrderedDict({"name": self.bucket})

            prometheus_labels = merge_dicts_ordered(
                prometheus_labels, prometheus_namespace_label
            )
            prometheus_labels = merge_dicts_ordered(
                prometheus_labels, prometheus_name_label
            )

            collector_metric_name = []
            collector_metric_name.append("webtech_s3")

            up_metric_name = []
            up_metric_description = (
                "Did the 'Webtech S3' Prometheus Exporter Up & Running."
            )
            up_metric_name.extend(collector_metric_name)
            up_metric_name.append("exporter")

            state_metric_name = []
            state_metric_description = (
                "Did the 'Webtech S3' specific Openshift Resource push succeed."
            )
            state_metric_name.extend(collector_metric_name)
            state_metric_name.append("exporter")
            state_metric_name.append("status")

            metrics.append(
                [
                    state_metric_name,
                    prometheus_labels,
                    int(COLLECTOR_STATE),
                    state_metric_description,
                ]
            )

            size_metric_name = []
            size_metric_description = "Size of S3 bucket."
            size_metric_name.extend(collector_metric_name)
            size_metric_name.append("bucket_size_kbytes")

            metrics.append(
                [
                    size_metric_name,
                    prometheus_labels,
                    int(SIZE_KBYTE),
                    size_metric_description,
                ]
            )

            count_metric_name = []
            count_metric_description = "Object Count of S3 bucket."
            count_metric_name.extend(collector_metric_name)
            count_metric_name.append("bucket_count_total")

            metrics.append(
                [
                    count_metric_name,
                    prometheus_labels,
                    int(COUNT_TOTAL),
                    count_metric_description,
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


def main():

    signal.signal(signal.SIGTERM, signal_handler)

    global SIZE_KBYTE
    global COUNT_TOTAL
    global COLLECTOR_STATE

    SIZE_KBYTE = 0
    COUNT_TOTAL = 0
    COLLECTOR_STATE = False

    openshift_namespace = os.getenv("OPENSHIFT_NAMESPACE", "")

    aws_access_key_id = os.getenv("AWS_ACCESS_KEY_ID", "")
    aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    aws_default_region = os.getenv("AWS_DEFAULT_REGION", "")
    aws_endpoint_url = os.getenv("AWS_ENDPOINT_URL", "")
    s3_bucket_name = os.getenv("S3_BUCKET_NAME", "")

    interval = 1

    if not len(openshift_namespace) > 0:
        print("Environment variables openshift_namespace cannot be null or undefined !")
        sys.exit()

    if not len(aws_access_key_id) > 0:
        print("Environment variables AWS_ACCESS_KEY_ID cannot be null or undefined !")
        sys.exit()

    if not len(aws_secret_access_key) > 0:
        print(
            "Environment variables AWS_SECRET_ACCESS_KEY cannot be null or undefined !"
        )
        sys.exit()

    if not len(aws_default_region) > 0:
        print("Environment variables AWS_DEFAULT_REGION cannot be null or undefined !")
        sys.exit()

    if not len(aws_endpoint_url) > 0:
        print("Environment variables AWS_ENDPOINT_URL cannot be null or undefined !")
        sys.exit()

    if not len(s3_bucket_name) > 0:
        print("Environment variables S3_BUCKET_NAME cannot be null or undefined !")
        sys.exit()

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

    with open("config/collector.yml", "r") as ymlfile:
        collector_configuration = yaml.load(ymlfile, Loader=yaml.SafeLoader)

    for c in collector_configuration["configuration"]:
        if "interval" in c:
            interval = c["interval"]

    s3bucket = getClient(
        aws_access_key_id,
        aws_secret_access_key,
        aws_default_region,
        aws_endpoint_url,
        s3_bucket_name,
    )

    i = Info("webtech_s3_exporter", "Webtech Prometheus Exporter version")
    i.info(
        {
            "version": __version__,
            "namespace": openshift_namespace,
            "name": s3_bucket_name,
        }
    )

    REGISTRY.register(
        BucketSizeCollector(namespace=openshift_namespace, bucket=s3_bucket_name)
    )

    exporter(port=9773)
    health(port=9774)

    try:
        while True:
            threads = []

            t = threading.Thread(target=collect, args=(s3bucket, s3_bucket_name))
            threads.append(t)

            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            del threads[:]

            time.sleep(interval * 60)

    except KeyboardInterrupt:
        pass

    shutdown()
