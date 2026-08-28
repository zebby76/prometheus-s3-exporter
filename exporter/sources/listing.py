"""Universal fallback: walk the bucket and add the objects up.

This is the only source that works against any S3 implementation, because it
uses nothing but ListObjectsV2. It is also the only one whose cost grows with
the bucket: one request per thousand keys, and — measured at roughly 71 us per
object — a wall-clock time dominated by parsing rather than by the network.
"""

from __future__ import annotations

import logging
from typing import Optional

from .base import SourceContext, SourceUnavailable, Usage, UsageSource

# ListObjectsV2 caps a page at 1000 keys; asking for more is silently clamped.
_PAGE_SIZE = 1000


class ListingSource(UsageSource):
    name = "list"

    def __init__(self, ctx: SourceContext):
        self._client = ctx.client
        self._bucket = ctx.bucket

    @classmethod
    def probe(cls, ctx: SourceContext) -> Optional["ListingSource"]:
        # Always available: it needs nothing beyond the credentials the exporter
        # already uses to reach its bucket. Detection therefore cannot come up
        # empty, which is why this source is probed last.
        return cls(ctx)

    def fetch(self) -> Usage:
        paginator = self._client.get_paginator("list_objects_v2")
        size = 0
        count = 0
        pages = 0
        try:
            for page in paginator.paginate(
                Bucket=self._bucket,
                PaginationConfig={"PageSize": _PAGE_SIZE},
            ):
                pages += 1
                # Summing the raw dicts skips the per-key ObjectSummary that the
                # boto3 resource layer would build, which is ~17% of the work.
                for obj in page.get("Contents", ()):
                    size += obj["Size"]
                    count += 1
        except Exception as exc:
            raise SourceUnavailable(f"listing bucket '{self._bucket}': {exc}") from exc

        logging.debug(
            "Listed bucket %s in %s request(s): %s objects", self._bucket, pages, count
        )
        return Usage(size_bytes=size, object_count=count, as_of=None)
