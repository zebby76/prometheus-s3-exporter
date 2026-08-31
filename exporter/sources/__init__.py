"""Usage source selection.

`auto` walks the candidates in order and keeps the first that answers, which
means the cheap provider-specific sources win when they are reachable and the
listing catches everything else. Detection can never come up empty because the
listing always probes successfully.
"""

from __future__ import annotations

import logging

from .base import SourceContext, SourceUnavailable, Usage, UsageSource
from .cloudwatch import CloudWatchSource
from .listing import ListingSource
from .minio import MinioSource
from .r2 import R2Source
from .storagegrid import StorageGridSource

AUTO = "auto"

# Order matters: constant-cost sources first, the linear one last.
SOURCES = (StorageGridSource, MinioSource, CloudWatchSource, R2Source, ListingSource)

__all__ = [
    "AUTO",
    "SOURCES",
    "CloudWatchSource",
    "ListingSource",
    "MinioSource",
    "R2Source",
    "SourceContext",
    "SourceUnavailable",
    "StorageGridSource",
    "Usage",
    "UsageSource",
    "resolve",
]


def _by_name(name: str):
    for candidate in SOURCES:
        if candidate.name == name:
            return candidate
    return None


def resolve(ctx: SourceContext, requested: str = AUTO) -> UsageSource:
    """Pick a usage source, honouring an explicit request when it works.

    An explicit source that fails to probe is reported as an error and we fall
    back to the listing rather than leaving the exporter with no data at all.
    The source actually in use is published as a label, so the substitution
    stays visible in Prometheus instead of hiding behind the configuration.
    """
    requested = (requested or AUTO).strip().lower()

    if requested != AUTO:
        wanted = _by_name(requested)
        if wanted is None:
            known = ", ".join(source.name for source in SOURCES)
            logging.error(
                "Unknown usage source '%s' (known: %s); falling back to detection",
                requested,
                known,
            )
        else:
            source = wanted.probe(ctx)
            if source is not None:
                logging.info("Using configured usage source: %s", source.name)
                return source
            logging.error(
                "Configured usage source '%s' is unavailable; falling back to '%s'",
                requested,
                ListingSource.name,
            )
            return ListingSource(ctx)

    for candidate in SOURCES:
        source = candidate.probe(ctx)
        if source is not None:
            logging.info("Detected usage source: %s", source.name)
            return source

    # Unreachable: ListingSource.probe never returns None. Kept so a future
    # source list without it still yields something usable.
    logging.warning("No usage source detected; using '%s'", ListingSource.name)
    return ListingSource(ctx)
