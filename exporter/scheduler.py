"""Who runs what, and when, for every watched bucket.

Two threads drive the monitors:

- the credentials refresher reads every bucket's Secret again on a period, and
  at once for a bucket whose key S3 just rejected;
- the collection scheduler hands due collections to a bounded pool of workers,
  the most overdue first.

The probes are not scheduled here: each runs on its own thread, so a listing
that holds every worker for half an hour never delays a reachability check.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Optional

# A collection this late with no worker free counts as overdue: the sign that
# max_concurrent_collections or the CPU limit no longer keeps up.
OVERDUE_GRACE_SECONDS = 60

# The longest the scheduler sleeps without looking, whatever is due next.
MAX_SLEEP_SECONDS = 60


class CollectionScheduler:
    def __init__(
        self,
        monitors: List,
        max_workers: int,
        stop: threading.Event,
        clock: Callable[[], float] = time.monotonic,
        executor=None,
    ):
        self.monitors = monitors
        self.max_workers = max_workers
        self._stop = stop
        self._clock = clock
        self._executor = executor or ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="collection"
        )
        self._lock = threading.Lock()
        self._running = 0
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def running(self) -> int:
        return self._running

    def overdue(self) -> int:
        late = self._clock() - OVERDUE_GRACE_SECONDS
        return sum(
            1
            for monitor in self.monitors
            if monitor.collectable() and not monitor.running and monitor.next_due < late
        )

    def wake(self):
        """Look again now: a collection ended, or a bucket became watchable."""
        self._wake.set()

    def step(self) -> float:
        """Start what is due; return how long to sleep before looking again."""
        now = self._clock()
        with self._lock:
            due = sorted(
                (
                    monitor
                    for monitor in self.monitors
                    if monitor.collectable()
                    and not monitor.running
                    and monitor.next_due <= now
                ),
                key=lambda monitor: monitor.next_due,
            )
            for monitor in due[: max(0, self.max_workers - self._running)]:
                monitor.running = True
                self._running += 1
                self._executor.submit(self._run, monitor)

            upcoming = [
                monitor.next_due
                for monitor in self.monitors
                if monitor.collectable() and not monitor.running
            ]
        if not upcoming:
            return MAX_SLEEP_SECONDS
        return min(MAX_SLEEP_SECONDS, max(0.05, min(upcoming) - now))

    def _run(self, monitor):
        try:
            monitor.collect()
        except Exception:
            logging.exception("Collection of %s failed", monitor.spec.key)
        finally:
            with self._lock:
                monitor.running = False
                self._running -= 1
            self.wake()

    def run(self):
        while not self._stop.is_set():
            delay = self.step()
            self._wake.wait(delay)
            self._wake.clear()

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(
            target=self.run, name="collection-scheduler", daemon=True
        )
        self._thread.start()
        return self._thread

    def shutdown(self):
        self._wake.set()
        # A listing in progress is not waited for: SIGTERM must not be held
        # for half an hour.
        self._executor.shutdown(wait=False, cancel_futures=True)


class CredentialsRefresher:
    """Reads every bucket's credentials again, periodically and on demand."""

    def __init__(
        self,
        monitors: List,
        interval_seconds: float,
        stop: threading.Event,
        on_change: Optional[Callable[[], None]] = None,
    ):
        self.monitors = monitors
        self.interval_seconds = interval_seconds
        self._stop = stop
        self.on_change = on_change
        self._requested = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def request(self):
        """A bucket's key was rejected: read the Secrets flagged as stale now."""
        self._requested.set()

    def _refresh(self, monitors):
        for monitor in monitors:
            if self._stop.is_set():
                return
            try:
                monitor.refresh_credentials()
            except Exception:
                logging.exception("Refreshing %s failed", monitor.spec.key)
        if self.on_change is not None:
            self.on_change()

    def refresh_all(self):
        self._refresh(self.monitors)

    def run(self):
        deadline = time.monotonic() + self.interval_seconds
        while not self._stop.is_set():
            requested = self._requested.wait(max(0, deadline - time.monotonic()))
            if self._stop.is_set():
                return
            if requested:
                self._requested.clear()
                self._refresh([m for m in self.monitors if m.needs_refresh])
            if time.monotonic() >= deadline:
                self.refresh_all()
                deadline = time.monotonic() + self.interval_seconds

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(
            target=self.run, name="credentials-refresher", daemon=True
        )
        self._thread.start()
        return self._thread
