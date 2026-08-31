"""Cloudflare R2: read the usage the account API already maintains.

R2 speaks S3, but it has none of the cheap usage paths the other providers offer:
no StorageGRID extension, no MinIO metrics endpoint, no CloudWatch. Walking a
bucket is therefore the only option *over S3* — and it is the expensive one, at
one request per thousand keys.

Cloudflare does publish the figures, just not on the S3 endpoint: the account API
answers `GET /accounts/{id}/r2/buckets/{bucket}/usage` with the object count and
the stored size in a single request, whatever the bucket holds. It authenticates
with a Cloudflare API token (`Workers R2 Storage: Read`), which is a different
credential from the S3 key pair — hence an opt-in source, like MinIO's.

The numbers are billing aggregates: they are computed on a schedule and come with
the `end` of the window they cover, so the lag is reported rather than guessed
(measured at 20 to 60 minutes on a live account). Read it on
`webtech_s3_usage_stale_seconds` and size the alert accordingly: this source
trades freshness for a constant cost, it does not improve on it.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from .base import SourceContext, SourceUnavailable, Usage, UsageSource

API_BASE = "https://api.cloudflare.com/client/v4"


def _int(payload: dict, key: str) -> int:
    """Read a counter. The API returns every figure as a decimal STRING."""
    raw = payload.get(key)
    if raw in (None, ""):
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise SourceUnavailable(f"field '{key}' is not a number: {raw!r}") from exc


def _as_of(raw) -> Optional[datetime]:
    """Parse the `end` of the aggregation window the figures describe."""
    if not raw:
        return None
    try:
        # Python < 3.11 cannot read the trailing Z that Cloudflare emits.
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        logging.debug("Unparsable usage window end: %r", raw)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_usage(document: dict, bucket: str) -> Usage:
    """Turn an API response into a Usage, or raise SourceUnavailable."""
    if not isinstance(document, dict) or not document.get("success", False):
        errors = "; ".join(
            str(e.get("message", e)) for e in (document or {}).get("errors") or []
        )
        raise SourceUnavailable(
            f"usage API refused bucket '{bucket}': {errors or 'no result'}"
        )

    result = document.get("result")
    if not isinstance(result, dict):
        raise SourceUnavailable(f"usage API returned no figures for bucket '{bucket}'")

    # Infrequent Access objects are objects like any other: ListObjectsV2 returns
    # them, so counting them here keeps this source comparable with the listing.
    # `metadataSize` is deliberately left out for the same reason — the listing
    # sums object payloads only, and the two sources must not disagree by design.
    size = _int(result, "payloadSize") + _int(result, "infrequentAccessPayloadSize")
    count = _int(result, "objectCount") + _int(result, "infrequentAccessObjectCount")

    return Usage(size_bytes=size, object_count=count, as_of=_as_of(result.get("end")))


class R2Source(UsageSource):
    name = "r2"

    def __init__(
        self,
        ctx: SourceContext,
        account_id: str,
        token: str,
        jurisdiction: Optional[str] = None,
        api_base: str = API_BASE,
    ):
        self._bucket = ctx.bucket
        self._timeout = ctx.timeout
        self._url = (
            f"{api_base.rstrip('/')}/accounts/{urllib.parse.quote(account_id)}"
            f"/r2/buckets/{urllib.parse.quote(ctx.bucket)}/usage"
        )
        self._headers = {"Authorization": f"Bearer {token}"}
        if jurisdiction:
            # A bucket created in a jurisdiction does not exist on the default
            # one: without this header the API answers 404, not a wrong figure.
            self._headers["cf-r2-jurisdiction"] = jurisdiction

    @staticmethod
    def _setting(config: dict, key: str, env: str) -> str:
        """Config first, environment second — the deployment decides which."""
        value = config.get(key)
        if value:
            return str(value)
        return os.getenv(env, "").strip()

    @classmethod
    def _token(cls, config: dict) -> str:
        token = config.get("api_token")
        if token:
            return str(token)
        token_file = config.get("api_token_file")
        if token_file:
            try:
                return Path(token_file).read_text(encoding="utf-8").strip()
            except OSError as exc:
                logging.error("Cannot read Cloudflare api_token_file: %s", exc)
                return ""
        return os.getenv("CF_API_TOKEN", "").strip()

    @classmethod
    def probe(cls, ctx: SourceContext) -> Optional["R2Source"]:
        try:
            config = ctx.source_config(cls.name)
        except ValueError as exc:
            logging.error("%s", exc)
            return None

        account_id = cls._setting(config, "account_id", "CF_ACCOUNT_ID")
        token = cls._token(config)
        if not account_id or not token:
            # Opt-in: the account API needs a credential the exporter is not
            # given by default, so silence here means "not configured", not
            # "broken". The listing stays available behind us.
            return None

        candidate = cls(
            ctx,
            account_id,
            token,
            cls._setting(config, "jurisdiction", "CF_R2_JURISDICTION") or None,
            str(config.get("api_base") or API_BASE),
        )
        try:
            candidate.fetch()
        except Exception as exc:
            logging.debug("Cloudflare R2 usage API unavailable: %s", exc)
            return None
        return candidate

    def fetch(self) -> Usage:
        try:
            response = requests.get(
                self._url, headers=self._headers, timeout=self._timeout
            )
        except requests.RequestException as exc:
            raise SourceUnavailable(f"usage request failed: {exc}") from exc

        if response.status_code != 200:
            hint = {
                401: " (token rejected)",
                403: " (token lacks Workers R2 Storage: Read)",
                404: " (unknown bucket, or wrong jurisdiction)",
            }.get(response.status_code, "")
            raise SourceUnavailable(
                f"usage request returned HTTP {response.status_code}{hint}"
            )

        try:
            document = response.json()
        except ValueError as exc:
            raise SourceUnavailable(f"usage response is not JSON: {exc}") from exc

        return parse_usage(document, self._bucket)
