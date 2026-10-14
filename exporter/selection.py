"""When to detect the usage source again.

Detection is run at startup and picks the cheapest source that answers. Two
things can make the exporter detect again later, and both used to be able to
leave it on the expensive listing for good:

- A run of failed collections. If the bucket itself is down, every
  constant-cost source fails its probe and the listing -- whose probe never
  checks anything -- wins; once the storage is back the listing succeeds, the
  failures stop, and nothing ever detects again. So a run of failures does not
  trigger detection while the connectivity probe says the bucket is
  unreachable: the source is not the problem, the storage is.
- Running on the fallback. Whatever brought the exporter down to the listing
  -- a start during an outage, an extension briefly unavailable, no probe to
  ask -- detection is retried every `redetect_seconds` while it is there, so
  the drop is never permanent.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from .sources import AUTO, ListingSource, UsageSource

# Seconds between two detections while the exporter runs on the listing
# fallback. One detection costs one request per candidate source.
DEFAULT_REDETECT_SECONDS = 3600


class SourceSelector:
    """Holds the active source and decides when to detect it again.

    `detect` runs a detection and returns the source it found; the caller owns
    what that involves (a fresh client, publishing the source name).
    """

    def __init__(
        self,
        detect: Callable[[], UsageSource],
        requested: str = AUTO,
        probe=None,
        max_failures: int = 3,
        redetect_seconds: float = DEFAULT_REDETECT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._detect = detect
        self._requested = (requested or AUTO).strip().lower()
        self._probe = probe
        self._max_failures = max_failures
        self._redetect_seconds = redetect_seconds
        self._clock = clock
        self._failures = 0
        self.source = detect()
        self._detected_at = clock()

    @property
    def on_fallback(self) -> bool:
        """On the listing although something cheaper was asked for."""
        return (
            self.source.name == ListingSource.name
            and self._requested != ListingSource.name
        )

    def _bucket_unreachable(self) -> bool:
        """True only when the probe has run and could not reach the bucket."""
        if self._probe is None:
            return False
        result = self._probe.result
        return result.checked_at is not None and not result.reachable

    def _redetect(self, reason: str) -> Optional[UsageSource]:
        previous = self.source
        self._detected_at = self._clock()
        try:
            self.source = self._detect()
        except Exception as e:
            # A client that cannot be rebuilt right now: keep collecting with
            # the source we have, and try again at the next occasion.
            logging.error("Cannot detect the usage source again: %s", e)
            return None
        if self.source.name != previous.name:
            logging.warning(
                "Usage source changed from '%s' to '%s' (%s)",
                previous.name,
                self.source.name,
                reason,
            )
        return self.source

    def redetect(self, reason: str) -> Optional[UsageSource]:
        """Detect again now, for a reason the caller knows about.

        A rotated credential, typically: the source detected with the old key
        may not be the one the new key can use.
        """
        self._failures = 0
        return self._redetect(reason)

    def record(self, succeeded: bool) -> Optional[UsageSource]:
        """Account for one collection; returns the source if it detected again."""
        if succeeded:
            self._failures = 0
            if (
                self.on_fallback
                and self._redetect_seconds
                and self._clock() - self._detected_at >= self._redetect_seconds
            ):
                return self._redetect("retrying detection from the listing fallback")
            return None

        self._failures += 1
        if self._failures < self._max_failures:
            return None

        failures, self._failures = self._failures, 0
        if self._bucket_unreachable():
            logging.warning(
                "%s consecutive failures while the bucket is unreachable; "
                "keeping the '%s' source until the storage is back",
                failures,
                self.source.name,
            )
            return None

        logging.warning(
            "%s consecutive failures, re-detecting the usage source", failures
        )
        return self._redetect(f"{failures} consecutive failures")
