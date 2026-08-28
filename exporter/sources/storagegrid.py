"""NetApp StorageGRID: the `x-ntap-sg-usage` S3 extension.

StorageGRID answers `GET /<bucket>?x-ntap-sg-usage` with figures its own scanner
maintains. The request is a plain signed S3 request against the bucket, so it
needs no permission beyond the ones the exporter already holds -- including a
credential scoped to a single bucket, which cannot reach the tenant-wide variant
on the service root.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import requests
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from .base import SourceContext, SourceUnavailable, Usage, UsageSource

# Present in every usage payload; its absence means the query parameter was
# ignored and we are talking to something that is not StorageGRID.
_MARKER = "calculationTime"


def is_json_response(content_type) -> bool:
    """Whether a response body is worth reading as a usage payload.

    A grid without the extension answers 200 with an XML ListBucketResult --
    up to 1000 objects, so about a megabyte. Deciding from the header lets us
    hang up before downloading it, which matters because the probe runs at
    every startup and every re-detection.
    """
    return "json" in (content_type or "").lower()


def _parse_calculation_time(raw) -> Optional[datetime]:
    """Best-effort ISO-8601 parse; the figures stay usable without it."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        logging.warning("StorageGRID returned an unparsable calculationTime: %r", raw)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_usage(payload, bucket: str) -> Usage:
    """Turn a usage payload into a Usage.

    Both shapes are accepted: the bucket-scoped answer carries the totals at the
    top level, while the tenant-scoped one nests them in a `buckets` list.
    """
    if not isinstance(payload, dict) or _MARKER not in payload:
        raise SourceUnavailable("response is not a StorageGRID usage payload")

    entry = payload
    buckets = payload.get("buckets")
    if isinstance(buckets, list):
        entry = next(
            (b for b in buckets if isinstance(b, dict) and b.get("name") == bucket),
            None,
        )
        if entry is None:
            raise SourceUnavailable(f"bucket '{bucket}' absent from usage payload")

    try:
        size = int(entry["dataBytes"])
        count = int(entry["objectCount"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceUnavailable(f"malformed usage payload: {exc}") from exc

    return Usage(
        size_bytes=size,
        object_count=count,
        as_of=_parse_calculation_time(payload.get(_MARKER)),
    )


class StorageGridSource(UsageSource):
    name = "storagegrid"

    def __init__(self, ctx: SourceContext):
        self._bucket = ctx.bucket
        self._timeout = ctx.timeout
        self._url = f"{ctx.endpoint_url.rstrip('/')}/{ctx.bucket}?x-ntap-sg-usage"
        # S3SigV4Auth, never the generic SigV4Auth: only the S3 variant emits
        # x-amz-content-sha256, without which StorageGRID answers 403. MinIO
        # accepts either, so the generic one appears to work in the dev stack.
        self._auth = S3SigV4Auth(
            Credentials(access_key=ctx.access_key, secret_key=ctx.secret_key),
            "s3",
            ctx.region,
        )

    def _request(self) -> requests.Response:
        signed = AWSRequest(method="GET", url=self._url)
        self._auth.add_auth(signed)
        # Streamed so the body is only pulled once the content type says it is
        # worth pulling.
        return requests.get(
            self._url,
            headers=dict(signed.headers),
            timeout=self._timeout,
            stream=True,
        )

    @classmethod
    def probe(cls, ctx: SourceContext) -> Optional["StorageGridSource"]:
        candidate = cls(ctx)
        try:
            candidate.fetch()
        except Exception as exc:
            logging.debug("StorageGRID usage extension unavailable: %s", exc)
            return None
        return candidate

    def fetch(self) -> Usage:
        try:
            with self._request() as response:
                if response.status_code != 200:
                    raise SourceUnavailable(
                        f"usage request returned HTTP {response.status_code}"
                    )

                content_type = response.headers.get("Content-Type")
                if not is_json_response(content_type):
                    # A 200 carrying XML means the query parameter was ignored
                    # and we were handed a plain object listing instead: the
                    # extension is not available on this grid.
                    raise SourceUnavailable(
                        f"usage response is {content_type or 'untyped'}, not JSON "
                        "(the x-ntap-sg-usage parameter was ignored)"
                    )

                try:
                    payload = response.json()
                except (json.JSONDecodeError, ValueError) as exc:
                    raise SourceUnavailable(
                        f"usage response is not valid JSON: {exc}"
                    ) from exc
        except requests.RequestException as exc:
            raise SourceUnavailable(f"usage request failed: {exc}") from exc

        return parse_usage(payload, self._bucket)
