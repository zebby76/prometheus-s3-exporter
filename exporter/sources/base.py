"""Building blocks shared by every usage source.

A usage source answers one question: how big is the bucket, and how many objects
does it hold. The listing walks the bucket to find out, which costs one request
per thousand objects; the provider-specific sources ask the storage system for a
figure it already maintains, which costs one request whatever the bucket size.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


class SourceUnavailable(Exception):
    """A source cannot serve usage here, or failed to do so."""


@dataclass(frozen=True)
class Usage:
    """One usage measurement.

    `as_of` is the moment the provider computed these figures. Providers that
    maintain the numbers with a background scanner report it, and it can lag by
    minutes (MinIO, StorageGRID) or by a day (CloudWatch). `None` means the
    measurement is live, which is the case when we walked the bucket ourselves.
    """

    size_bytes: int
    object_count: int
    as_of: Optional[datetime] = None

    def stale_seconds(self, now: Optional[datetime] = None) -> float:
        """Age of the measurement in seconds; 0 when it was taken live."""
        if self.as_of is None:
            return 0.0
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - self.as_of).total_seconds())


@dataclass(frozen=True)
class SourceContext:
    """Everything a source may need, resolved once at startup."""

    endpoint_url: str
    region: str
    bucket: str
    access_key: str
    secret_key: str
    client: Any
    timeout: int = 30
    config: dict = field(default_factory=dict)

    def source_config(self, name: str) -> dict:
        """Return the `sources.<name>` sub-tree, defaulting to empty."""
        value = self.config.get(name)
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(f"configuration for source '{name}' must be a mapping")
        return value


class UsageSource(ABC):
    """A backend able to report bucket usage."""

    name = "abstract"

    @classmethod
    @abstractmethod
    def probe(cls, ctx: SourceContext) -> Optional["UsageSource"]:
        """Return a ready instance if this source works here, else None.

        A probe must be cheap and must not raise: detection runs at startup and
        walks every candidate in turn.
        """

    @abstractmethod
    def fetch(self) -> Usage:
        """Return the current usage, or raise SourceUnavailable."""
