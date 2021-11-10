import os
import sys
import yaml
import logging
import time
import re
import signal
import argparse
import threading
import requests
import json
import hashlib
import ast
import time

from datetime import date

from collections import OrderedDict
from .utils import merge_dicts_ordered

from prometheus_client import Gauge, Info
from prometheus_client.core import InfoMetricFamily, GaugeMetricFamily, REGISTRY

from exporter.handler import exporter, health

import boto3

gauges = {}

metric_invalid_chars = re.compile(r"[^a-zA-Z0-9_:]")
metric_invalid_start_chars = re.compile(r"^[^a-zA-Z_:]")
label_invalid_chars = re.compile(r"[^a-zA-Z0-9_]")
label_invalid_start_chars = re.compile(r"^[^a-zA-Z_]")
label_start_double_under = re.compile(r"^__+")

def shutdown():
    logging.info("Shutting down")
    sys.exit(1)


def signal_handler(signum, frame):
    shutdown()


def getClient(AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION, AWS_ENDPOINT_URL, S3_BUCKET_NAME):

    try:

        s3 = boto3.resource(
          service_name = 's3',
          region_name = AWS_DEFAULT_REGION,
          endpoint_url = AWS_ENDPOINT_URL,
          aws_access_key_id = AWS_ACCESS_KEY_ID,
          aws_secret_access_key = AWS_SECRET_ACCESS_KEY
        )

        bucket = s3.Bucket(S3_BUCKET_NAME)

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
    metric = '_'.join(name_list)
    metric = re.sub(metric_invalid_chars, '_', metric)
    metric = re.sub(metric_invalid_start_chars, '_', metric)
    return metric.lower()


def group_metrics(metrics):

    metric_dict = {}
    for (name_list, label_dict, value, description) in metrics:
        metric_name = format_metric_name(name_list)
        label_dict = OrderedDict([(format_label_key(k), format_label_value(v))
                                  for k, v in label_dict.items()])

        if metric_name not in metric_dict:
            metric_dict[metric_name] = (tuple(label_dict.keys()), {}, {"description": description})

        label_keys = metric_dict[metric_name][0]
        label_values = tuple([label_dict[key]
                              for key in label_keys])

        metric_dict[metric_name][1][label_values] = value

    logging.debug("Function [group_metrics] - return : %s" % (metric_dict))
    return metric_dict


def gauge_generator(metrics):
    metric_dict = group_metrics(metrics)

    for metric_name, (label_keys, value_dict, description) in metric_dict.items():
        if label_keys:
            gauge = GaugeMetricFamily(metric_name, description["description"], labels=label_keys)

            for label_values, value in value_dict.items():
                gauge.add_metric(label_values, value)
        else:
            gauge = GaugeMetricFamily(metric_name, description["description"], value=list(value_dict.values())[0])

        yield gauge


def collector_up_gauge(name_list, description, succeeded=True):
    metric_name = format_metric_name(name_list + ["up"])
    return GaugeMetricFamily(metric_name, description, value=int(succeeded))


def collect(s3bucket, S3_BUCKET_NAME):

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
        logging.info("Collect S3 Bucket %s - {'size':%s,'count':%s}",S3_BUCKET_NAME,int(SIZE_KBYTE),int(COUNT_TOTAL))

class BucketSizeCollector(object):
    def __init__(
        self,
        namespace,
        bucket
    ):
        self.namespace = namespace
        self.bucket = bucket

    def collect(self):

        metrics = []

        try:

            prometheus_labels = {}

            prometheus_namespace_label = OrderedDict({"namespace": self.namespace})
            prometheus_name_label = OrderedDict({"name": self.bucket})
            
            prometheus_labels = merge_dicts_ordered(prometheus_labels, prometheus_namespace_label)
            prometheus_labels = merge_dicts_ordered(prometheus_labels, prometheus_name_label)

            collector_metric_name = []
            collector_metric_name.append("webtech_s3")

            up_metric_name = []
            up_metric_description = "Did the 'Webtech S3' Prometheus Exporter Up & Running."
            up_metric_name.extend(collector_metric_name)
            up_metric_name.append("exporter")

            state_metric_name = []
            state_metric_description = "Did the 'Webtech S3' specific Openshift Resource push succeed."
            state_metric_name.extend(collector_metric_name)
            state_metric_name.append("exporter")
            state_metric_name.append("status")

            metrics.append([state_metric_name, prometheus_labels, int(COLLECTOR_STATE), state_metric_description])

            size_metric_name = []
            size_metric_description = "Size of S3 bucket."
            size_metric_name.extend(collector_metric_name)
            size_metric_name.append("bucket_size_kbytes")

            metrics.append([size_metric_name, prometheus_labels, int(SIZE_KBYTE), size_metric_description])

            count_metric_name = []
            count_metric_description = "Object Count of S3 bucket."
            count_metric_name.extend(collector_metric_name)
            count_metric_name.append("bucket_count_total")

            metrics.append([count_metric_name, prometheus_labels, int(COUNT_TOTAL), count_metric_description])

        except Exception as e:
            logging.error(e)
            yield collector_up_gauge(up_metric_name, up_metric_description, succeeded=False)
        else:
            yield from gauge_generator(metrics)
            yield collector_up_gauge(up_metric_name, up_metric_description, succeeded=True)


def main():

    signal.signal(signal.SIGTERM, signal_handler)

    global SIZE_KBYTE
    global COUNT_TOTAL
    global COLLECTOR_STATE

    SIZE_KBYTE = 0
    COUNT_TOTAL = 0
    COLLECTOR_STATE = False

    OPENSHIFT_NAMESPACE = os.getenv("OPENSHIFT_NAMESPACE", "")

    AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    AWS_DEFAULT_REGION = os.getenv("AWS_DEFAULT_REGION", "")
    AWS_CLI_EXTRA_ARGS = os.getenv("AWS_CLI_EXTRA_ARGS", "")
    AWS_ENDPOINT_URL = os.getenv("AWS_ENDPOINT_URL", "")

    S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "")

    INTERVAL = 1

    if not len(OPENSHIFT_NAMESPACE) > 0:
        print(
            "Environment variables OPENSHIFT_NAMESPACE cannot be null or undefined !"
        )
        sys.exit()

    if not len(AWS_ACCESS_KEY_ID) > 0:
        print(
            "Environment variables AWS_ACCESS_KEY_ID cannot be null or undefined !"
        )
        sys.exit()

    if not len(AWS_SECRET_ACCESS_KEY) > 0:
        print(
            "Environment variables AWS_SECRET_ACCESS_KEY cannot be null or undefined !"
        )
        sys.exit()

    if not len(AWS_DEFAULT_REGION) > 0:
        print(
            "Environment variables AWS_DEFAULT_REGION cannot be null or undefined !"
        )
        sys.exit()

    if not len(AWS_ENDPOINT_URL) > 0:
        print(
            "Environment variables AWS_ENDPOINT_URL cannot be null or undefined !"
        )
        sys.exit()

    if not len(S3_BUCKET_NAME) > 0:
        print(
            "Environment variables S3_BUCKET_NAME cannot be null or undefined !"
        )
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
    logging.basicConfig(handlers=[log_handler], level=logging.DEBUG if args.verbose else log_level)
    logging.captureWarnings(True)

    with open("config/collector.yml", "r") as ymlfile:
        collector_configuration = yaml.load(ymlfile,Loader=yaml.SafeLoader)

    for c in collector_configuration['configuration']:
        if "interval" in c:
            INTERVAL = c['interval']

    s3bucket = getClient(AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION, AWS_ENDPOINT_URL, S3_BUCKET_NAME)

    i = Info('webtech_s3_exporter', 'Webtech Prometheus Exporter version')
    i.info({'version': os.getenv('VERSION', 'snapshot'),'namespace': OPENSHIFT_NAMESPACE, 'name': S3_BUCKET_NAME})

    REGISTRY.register(
        BucketSizeCollector(
            namespace=OPENSHIFT_NAMESPACE,
            bucket=S3_BUCKET_NAME
        )
    )

    exporter(port=9773)
    health(port=9774)

    try:
        while True:
            threads = []

            t = threading.Thread(target=collect, args=(s3bucket, S3_BUCKET_NAME))
            threads.append(t)

            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            del threads[:]

            time.sleep(INTERVAL * 60)

    except KeyboardInterrupt:
        pass

    shutdown()
