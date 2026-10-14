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
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

from exporter.errors import REASONS, classify, describe

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
    # One of exporter.errors.REASONS when the check failed.
    reason: Optional[str] = None

    def age_seconds(self, now: Optional[datetime] = None) -> float:
        """Seconds since this check ran; 0 before the first one."""
        if self.checked_at is None:
            return 0.0
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - self.checked_at).total_seconds())


class ConnectivityProbe:
    """Answers "can we still reach the bucket" with one cheap request."""

    def __init__(self, client, bucket, interval_seconds=60, on_error=None, name=None):
        self._client = client
        self._bucket = bucket
        # What the thread is named after: the bucket, or more when the same
        # bucket is watched twice.
        self._name = name or bucket
        self.interval_seconds = interval_seconds
        # Called with each failed attempt's exception, so the owner can tell a
        # rejected credential from an outage. Must not raise.
        self._on_error = on_error
        self._result = ProbeResult()
        # None until the first check decides which call this credential is
        # allowed to make; then it stays put. The label names it in the logs.
        self._method = None
        self._method_label = None
        # Monotonic, and deliberately not reset by set_client(): a counter that
        # went backwards on a client rebuild would break rate() over it. Same
        # for every counter below.
        self._retries_total = 0
        # Every failed attempt, retried or not, by where it failed.
        self._errors_total = {reason: 0 for reason in REASONS}
        # Sockets seen in the client's pool at the end of the last check; one
        # that was not there is a connection opened since. None until the pool
        # has been readable once.
        self._known_sockets = weakref.WeakSet()
        self._connections_total: Optional[int] = None

    @property
    def result(self) -> ProbeResult:
        return self._result

    @property
    def retries_total(self) -> int:
        """Immediate retries performed since start, across all checks."""
        return self._retries_total

    @property
    def errors_total(self) -> Dict[str, int]:
        """Failed attempts since start, by reason; every reason present."""
        return dict(self._errors_total)

    @property
    def connections_total(self) -> Optional[int]:
        """TCP connections the probe established since start.

        None when the client does not let us see them. A connection is reused
        between checks; a new one means the previous was closed -- by an idle
        timeout, typically -- and only a new one resolves the endpoint name
        again.
        """
        return self._connections_total

    def set_client(self, client):
        """Adopt a rebuilt S3 client, re-deciding which call to use."""
        self._client = client
        self._method = None
        self._method_label = None

    def _pooled_sockets(self) -> Optional[list]:
        """Sockets currently held by the client's connection pools, or None.

        Sockets and not urllib3's `num_connections`: that counts connection
        objects, and urllib3 reconnects a dropped one in place, on a new socket
        under the same object -- precisely the reconnection worth counting.
        Read through botocore's private session: best effort, and None rather
        than an exception when the layout is not the one boto3 1.39 has.
        """
        try:
            pools = self._client._endpoint.http_session._manager.pools
            sockets = []
            for key in list(pools.keys()):
                for conn in list(pools[key].pool.queue):
                    sock = getattr(conn, "sock", None)
                    if sock is not None:
                        sockets.append(sock)
            return sockets
        except Exception:
            return None

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
        # (seconds, exception) for each attempt that failed.
        failures = []
        attempts = 0

        while attempts < _MAX_ATTEMPTS:
            attempts += 1
            attempt_started = time.perf_counter()
            try:
                self._attempt()
                break
            except Exception as exc:
                failures.append((time.perf_counter() - attempt_started, exc))
                self._errors_total[classify(exc)] += 1
                if self._on_error is not None:
                    try:
                        self._on_error(exc)
                    except Exception:
                        logging.exception("Probe error callback failed")
                if attempts < _MAX_ATTEMPTS:
                    # Counted, not hidden: a probe that had to reconnect is a
                    # fact about the path, and the whole point of doing this
                    # here rather than in botocore is that it stays visible.
                    self._retries_total += 1

        duration = time.perf_counter() - started
        failed = len(failures) == attempts
        new_connections = self._count_connections()

        error = reason = None
        if failed:
            # Every attempt, each with its own time and root cause: the first
            # one is often the slow one, and the last alone used to be logged.
            error = "; ".join(
                f"#{n} {seconds:.3f}s {describe(exc)}"
                for n, (seconds, exc) in enumerate(failures, start=1)
            )
            reason = classify(failures[-1][1])

        result = ProbeResult(
            reachable=not failed,
            duration_seconds=duration,
            checked_at=datetime.now(timezone.utc),
            error=error,
            attempts=attempts,
            reason=reason,
        )

        previous = self._result
        self._result = result

        # Whichever call was attempted; on a first check that failed outright,
        # nothing was settled and HeadBucket is what was tried.
        method = self._method_label or "HeadBucket"

        if not failed:
            notes = []
            if new_connections:
                notes.append(
                    f"{new_connections} new connection"
                    f"{'' if new_connections == 1 else 's'}"
                )
            notes.extend(
                f"#{n} failed in {seconds:.3f}s: {describe(exc)}"
                for n, (seconds, exc) in enumerate(failures, start=1)
            )
            # A reused connection and a first-try success is the steady state:
            # debug, since probe_duration_seconds already carries the number.
            # Anything else is what explains a peak on that graph: info.
            logging.log(
                logging.INFO if notes else logging.DEBUG,
                "Probe S3 Bucket %s via %s in %.3fs (%s attempt%s)%s",
                self._bucket,
                method,
                duration,
                attempts,
                "" if attempts == 1 else "s",
                f": {'; '.join(notes)}" if notes else "",
            )

        if failed and previous.reachable:
            logging.error(
                "Bucket %s became unreachable via %s after %.3fs and %s attempts "
                "[%s]: %s",
                self._bucket,
                method,
                duration,
                attempts,
                reason,
                error,
            )
        elif not failed and previous.checked_at is not None and not previous.reachable:
            logging.info("Bucket %s is reachable again", self._bucket)
        elif failed:
            # Repeats stay at debug: an outage lasting hours would otherwise
            # fill the log with one identical error per interval.
            logging.debug(
                "Bucket %s still unreachable via %s after %.3fs and %s attempts "
                "[%s]: %s",
                self._bucket,
                method,
                duration,
                attempts,
                reason,
                error,
            )

        return result

    def _count_connections(self) -> int:
        """Connections opened since the last check, added to the total."""
        sockets = self._pooled_sockets()
        if sockets is None:
            return 0
        new = sum(1 for sock in sockets if sock not in self._known_sockets)
        self._known_sockets = weakref.WeakSet(sockets)
        self._connections_total = (self._connections_total or 0) + new
        return new

    def run_until(self, stop: threading.Event, initial_delay: float = 0):
        """Probe on the configured period until `stop` is set.

        `initial_delay` spreads the first checks of many buckets over the
        first interval, instead of a hundred requests in the same second.
        """
        if initial_delay and stop.wait(initial_delay):
            return
        while not stop.is_set():
            self.check_once()
            stop.wait(self.interval_seconds)

    def start(
        self, stop: threading.Event, initial_delay: float = 0
    ) -> Optional[threading.Thread]:
        """Start the probe thread, or return None when disabled."""
        if not self.interval_seconds:
            logging.info("Connectivity probe disabled")
            return None

        thread = threading.Thread(
            target=self.run_until,
            args=(stop, initial_delay),
            # Named after the bucket: with a hundred probes in one process,
            # the thread name is what a log line is read by.
            name=f"probe-{self._name}",
            daemon=True,
        )
        thread.start()
        logging.info(
            "Connectivity probe of %s started, every %ss",
            self._bucket,
            self.interval_seconds,
        )
        return thread
