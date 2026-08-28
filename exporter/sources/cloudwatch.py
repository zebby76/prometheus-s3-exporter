"""AWS S3: the daily storage metrics CloudWatch publishes for free.

BucketSizeBytes and NumberOfObjects cost one API call each whatever the bucket
size, but they are *daily* figures and lag by 24 to 48 hours -- hence the
`as_of` we report alongside them. Reading them calls a different AWS service, so
the credential needs `cloudwatch:GetMetricStatistics` in its IAM policy; an
S3-only key gets AccessDenied. This source is therefore opt-in.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import boto3

from .base import SourceContext, SourceUnavailable, Usage, UsageSource

_NAMESPACE = "AWS/S3"
# Storage metrics land once a day; look back far enough to survive a publishing
# hiccup, and keep the most recent datapoint we find.
_PERIOD_SECONDS = 86400
_LOOKBACK_DAYS = 4


def _latest(datapoints) -> Optional[dict]:
    if not datapoints:
        return None
    return max(datapoints, key=lambda point: point["Timestamp"])


class CloudWatchSource(UsageSource):
    name = "cloudwatch"

    def __init__(self, ctx: SourceContext, client, storage_type: str):
        self._bucket = ctx.bucket
        self._client = client
        self._storage_type = storage_type

    def _statistic(self, metric: str, storage_type: str) -> Optional[dict]:
        now = datetime.now(timezone.utc)
        response = self._client.get_metric_statistics(
            Namespace=_NAMESPACE,
            MetricName=metric,
            Dimensions=[
                {"Name": "BucketName", "Value": self._bucket},
                {"Name": "StorageType", "Value": storage_type},
            ],
            StartTime=now - timedelta(days=_LOOKBACK_DAYS),
            EndTime=now,
            Period=_PERIOD_SECONDS,
            Statistics=["Average"],
        )
        return _latest(response.get("Datapoints", []))

    @classmethod
    def probe(cls, ctx: SourceContext) -> Optional["CloudWatchSource"]:
        try:
            config = ctx.source_config(cls.name)
        except ValueError as exc:
            logging.error("%s", exc)
            return None

        if not config:
            # Opt-in: the extra IAM permission has to be granted deliberately,
            # so we never probe CloudWatch unless it was configured.
            return None

        try:
            client = boto3.client(
                "cloudwatch",
                region_name=config.get("region") or ctx.region,
                aws_access_key_id=config.get("access_key_id") or ctx.access_key,
                aws_secret_access_key=config.get("secret_access_key") or ctx.secret_key,
                endpoint_url=config.get("endpoint_url") or None,
            )
        except Exception as exc:
            logging.error("Cannot build a CloudWatch client: %s", exc)
            return None

        candidate = cls(ctx, client, str(config.get("storage_type", "StandardStorage")))
        try:
            candidate.fetch()
        except Exception as exc:
            logging.debug("CloudWatch storage metrics unavailable: %s", exc)
            return None
        return candidate

    def fetch(self) -> Usage:
        try:
            size_point = self._statistic("BucketSizeBytes", self._storage_type)
            # NumberOfObjects is only ever published under AllStorageTypes.
            count_point = self._statistic("NumberOfObjects", "AllStorageTypes")
        except Exception as exc:
            raise SourceUnavailable(f"CloudWatch query failed: {exc}") from exc

        if size_point is None or count_point is None:
            raise SourceUnavailable(
                f"no storage datapoint for bucket '{self._bucket}' "
                f"in the last {_LOOKBACK_DAYS} days"
            )

        as_of = min(size_point["Timestamp"], count_point["Timestamp"])
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)

        return Usage(
            size_bytes=int(size_point["Average"]),
            object_count=int(count_point["Average"]),
            as_of=as_of,
        )
