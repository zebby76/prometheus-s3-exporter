"""MinIO: read the usage its own Prometheus endpoint already publishes.

MinIO computes bucket usage with a background scanner and exposes it. Note that
this endpoint is not part of the S3 API: it authenticates with a JWT bearer
token minted from *admin* credentials, or not at all when the operator sets
MINIO_PROMETHEUS_AUTH_TYPE=public. An S3 access key cannot sign for it, which is
why this source stays opt-in and only activates once `metrics_url` is set.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from .base import SourceContext, SourceUnavailable, Usage, UsageSource

# name{label="value",...} 1.234e+05
_SAMPLE = re.compile(
    r"^(?P<name>[a-zA-Z_:][\w:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>[^\s]+)$"
)
_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')

# v2 (/minio/v2/metrics/bucket) and v3 (/minio/metrics/v3/cluster/usage/buckets)
# spell the same figures differently; both are accepted.
_SIZE_METRICS = (
    "minio_bucket_usage_total_bytes",
    "minio_cluster_usage_buckets_total_bytes",
)
_COUNT_METRICS = (
    "minio_bucket_usage_object_total",
    "minio_cluster_usage_buckets_objects_count",
)
_AGE_METRICS = (
    "minio_bucket_usage_last_activity_nano_seconds",
    "minio_cluster_usage_buckets_since_last_update_seconds",
)

# Both age metrics were observed reporting nanoseconds on MinIO
# RELEASE.2025-09-07, including the one whose name says seconds. Anything above
# this bound is therefore read as nanoseconds rather than as a 29-year-old scan.
_AGE_NS_THRESHOLD = 1e6


def parse_exposition(text: str):
    """Parse a Prometheus text exposition into [(name, labels, value)]."""
    samples = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE.match(line)
        if not match:
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        labels = dict(_LABEL.findall(match.group("labels") or ""))
        samples.append((match.group("name"), labels, value))
    return samples


def _pick(samples, names, bucket) -> Optional[float]:
    """First sample matching one of `names`, and this bucket if it is labelled."""
    for name, labels, value in samples:
        if name not in names:
            continue
        if "bucket" in labels and labels["bucket"] != bucket:
            continue
        return value
    return None


def _age_to_seconds(raw: Optional[float]) -> Optional[float]:
    if raw is None or raw < 0:
        return None
    return raw / 1e9 if raw > _AGE_NS_THRESHOLD else raw


def parse_usage(text: str, bucket: str, now: Optional[datetime] = None) -> Usage:
    samples = parse_exposition(text)
    size = _pick(samples, _SIZE_METRICS, bucket)
    count = _pick(samples, _COUNT_METRICS, bucket)
    if size is None or count is None:
        # The scanner may simply not have run yet on a fresh server: the
        # endpoint answers 200 with the usage metrics missing entirely.
        raise SourceUnavailable(f"no usage metrics published for bucket '{bucket}'")

    age = _age_to_seconds(_pick(samples, _AGE_METRICS, bucket))
    as_of = None
    if age is not None:
        as_of = (now or datetime.now(timezone.utc)) - timedelta(seconds=age)

    return Usage(size_bytes=int(size), object_count=int(count), as_of=as_of)


class MinioSource(UsageSource):
    name = "minio"

    def __init__(self, ctx: SourceContext, metrics_url: str, token: Optional[str]):
        self._bucket = ctx.bucket
        self._timeout = ctx.timeout
        self._url = metrics_url
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    @staticmethod
    def _token(config: dict) -> Optional[str]:
        token = config.get("bearer_token")
        if token:
            return str(token)
        token_file = config.get("bearer_token_file")
        if token_file:
            try:
                return Path(token_file).read_text(encoding="utf-8").strip()
            except OSError as exc:
                logging.error("Cannot read MinIO bearer_token_file: %s", exc)
        return None

    @classmethod
    def probe(cls, ctx: SourceContext) -> Optional["MinioSource"]:
        try:
            config = ctx.source_config(cls.name)
        except ValueError as exc:
            logging.error("%s", exc)
            return None

        metrics_url = config.get("metrics_url")
        if not metrics_url:
            # Opt-in: without an explicit endpoint there is nothing to try, and
            # guessing one would probe a host the operator never pointed us at.
            return None

        candidate = cls(ctx, str(metrics_url), cls._token(config))
        try:
            candidate.fetch()
        except Exception as exc:
            logging.debug("MinIO usage metrics unavailable: %s", exc)
            return None
        return candidate

    def fetch(self) -> Usage:
        try:
            response = requests.get(
                self._url, headers=self._headers, timeout=self._timeout
            )
        except requests.RequestException as exc:
            raise SourceUnavailable(f"metrics request failed: {exc}") from exc

        if response.status_code != 200:
            hint = (
                " (endpoint needs a bearer token or public auth)"
                if response.status_code == 403
                else ""
            )
            raise SourceUnavailable(
                f"metrics request returned HTTP {response.status_code}{hint}"
            )

        return parse_usage(response.text, self._bucket)
