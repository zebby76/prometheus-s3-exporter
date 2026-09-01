"""Bucket reachability, checked on its own clock.

The collection interval is sized for the cost of measuring a bucket, which on a
large one means tens of minutes. Reachability has to be answered far more often
than that, so it runs in its own thread rather than riding along with the
collection -- and never from the scrape handler, where a hung check would time
out the whole scrape and take every other metric down with it.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

# Errors that mean "you may not do this", as opposed to "the bucket is not
# there". They send us to the fallback instead of reporting an outage.
#
# Both spellings are needed. A HEAD reply carries no body, so botocore has no
# XML to read and fills in the HTTP status instead -- a denied HeadBucket comes
# back as "403", never as "AccessDenied". The names still matter for providers
# that answer a HEAD with a body anyway. 404 is deliberately absent: a missing
# bucket is an outage, not a permission problem.
_FORBIDDEN_CODES = frozenset(
    {
        "AccessDenied",
        "MethodNotAllowed",
        "NotImplemented",
        "403",
        "405",
        "501",
    }
)


# One immediate retry, and no more. A load balancer that drops idle connections
# leaves a dead socket in the pool, and the first write on it fails; reopening
# costs one connection setup -- ~120ms on a local grid -- against which the
# alternative, letting botocore retry, costs a random sleep of up to a second
# that is recorded as latency. Retrying anything rather than just connection
# errors keeps this honest: one extra request a cycle is not worth the fragility
# of matching exception types.
_MAX_ATTEMPTS = 2


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one reachability check.

    Frozen and replaced wholesale, so a scrape either sees the previous result
    or the next one, never a half-written mix.
    """

    reachable: bool = False
    duration_seconds: float = 0.0
    checked_at: Optional[datetime] = None
    error: Optional[str] = None
    attempts: int = 0

    def age_seconds(self, now: Optional[datetime] = None) -> float:
        """Seconds since this check ran; 0 before the first one."""
        if self.checked_at is None:
            return 0.0
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - self.checked_at).total_seconds())


class ConnectivityProbe:
    """Answers "can we still reach the bucket" with one cheap request."""

    def __init__(self, client, bucket, interval_seconds=60):
        self._client = client
        self._bucket = bucket
        self.interval_seconds = interval_seconds
        self._result = ProbeResult()
        # None until the first check decides which call this credential is
        # allowed to make; then it stays put. The label names it in the logs.
        self._method = None
        self._method_label = None
        # Monotonic, and deliberately not reset by set_client(): a counter that
        # went backwards on a client rebuild would break rate() over it.
        self._retries_total = 0

    @property
    def result(self) -> ProbeResult:
        return self._result

    @property
    def retries_total(self) -> int:
        """Immediate retries performed since start, across all checks."""
        return self._retries_total

    def set_client(self, client):
        """Adopt a rebuilt S3 client, re-deciding which call to use."""
        self._client = client
        self._method = None
        self._method_label = None

    def _head_bucket(self):
        self._client.head_bucket(Bucket=self._bucket)

    def _list_one(self):
        # Same permission as the listing the exporter already performs, and
        # still a single request with an almost empty body.
        self._client.list_objects_v2(Bucket=self._bucket, MaxKeys=1)

    @staticmethod
    def _error_code(exc) -> str:
        response = getattr(exc, "response", None) or {}
        return str(response.get("Error", {}).get("Code", ""))

    def _attempt(self):
        """One call, settling which one this credential may make on the first."""
        if self._method is None:
            # HeadBucket is the cheapest probe, but a bucket-scoped policy
            # may not grant it. Settle the question once.
            try:
                self._head_bucket()
                self._method = self._head_bucket
                self._method_label = "HeadBucket"
                logging.info("Connectivity probe using HeadBucket")
                return
            except Exception as exc:
                if self._error_code(exc) not in _FORBIDDEN_CODES:
                    raise
                logging.info(
                    "Connectivity probe falling back to ListObjectsV2: "
                    "HeadBucket denied (%s)",
                    self._error_code(exc),
                )
                self._method = self._list_one
                self._method_label = "ListObjectsV2"

        self._method()

    def check_once(self) -> ProbeResult:
        """Run one check and publish it. Never raises."""
        started = time.perf_counter()
        error = None
        attempts = 0

        while attempts < _MAX_ATTEMPTS:
            attempts += 1
            try:
                self._attempt()
                error = None
                break
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                if attempts < _MAX_ATTEMPTS:
                    # Counted, not hidden: a probe that had to reconnect is a
                    # fact about the path, and the whole point of doing this
                    # here rather than in botocore is that it stays visible.
                    self._retries_total += 1

        duration = time.perf_counter() - started
        result = ProbeResult(
            reachable=error is None,
            duration_seconds=duration,
            checked_at=datetime.now(timezone.utc),
            error=error,
            attempts=attempts,
        )

        previous = self._result
        self._result = result

        # Whichever call was attempted; on a first check that failed outright,
        # nothing was settled and HeadBucket is what was tried.
        method = self._method_label or "HeadBucket"

        if error is None:
            # Debug, not info: one line per interval per instance is a lot of
            # log for a number the probe_duration_seconds gauge already carries.
            logging.debug(
                "Probe S3 Bucket %s via %s in %.3fs (%s attempt%s)",
                self._bucket,
                method,
                duration,
                attempts,
                "" if attempts == 1 else "s",
            )

        if error is not None and previous.reachable:
            logging.error(
                "Bucket %s became unreachable via %s after %.3fs and %s attempts: %s",
                self._bucket,
                method,
                duration,
                attempts,
                error,
            )
        elif (
            error is None and previous.checked_at is not None and not previous.reachable
        ):
            logging.info("Bucket %s is reachable again", self._bucket)
        elif error is not None:
            # Repeats stay at debug: an outage lasting hours would otherwise
            # fill the log with one identical error per interval.
            logging.debug(
                "Bucket %s still unreachable via %s after %.3fs and %s attempts: %s",
                self._bucket,
                method,
                duration,
                attempts,
                error,
            )

        return result

    def run_until(self, stop: threading.Event):
        """Probe on the configured period until `stop` is set."""
        while not stop.is_set():
            self.check_once()
            stop.wait(self.interval_seconds)

    def start(self, stop: threading.Event) -> Optional[threading.Thread]:
        """Start the probe thread, or return None when disabled."""
        if not self.interval_seconds:
            logging.info("Connectivity probe disabled")
            return None

        thread = threading.Thread(
            target=self.run_until,
            args=(stop,),
            name="connectivity-probe",
            daemon=True,
        )
        thread.start()
        logging.info("Connectivity probe started, every %ss", self.interval_seconds)
        return thread
