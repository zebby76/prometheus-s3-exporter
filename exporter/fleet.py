"""The watched buckets, as a set a configuration reload can change.

The collection scheduler, the credentials refresher, the Prometheus collector
and /readyz all walk the same buckets. They iterate a Fleet as they would a
list; each iteration is a copy taken under the lock, so a reload that adds or
removes buckets never changes a list under someone's feet.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, Iterator, List, Optional

from exporter.buckets import BucketSpec


@dataclass
class FleetChange:
    """What a reload did, by bucket key."""

    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    changed: List[str] = field(default_factory=list)
    unchanged: int = 0

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.changed)

    def summary(self) -> str:
        return (
            f"{len(self.added)} added, {len(self.removed)} removed, "
            f"{len(self.changed)} changed, {self.unchanged} unchanged"
        )


class Fleet:
    """Every BucketMonitor, by its spec's key."""

    def __init__(
        self,
        monitors: Iterable,
        make_monitor: Callable[[BucketSpec], object],
        endpoints=None,
        on_change: Optional[Callable[[], None]] = None,
    ):
        self._lock = threading.Lock()
        self._monitors: Dict[str, object] = {m.spec.key: m for m in monitors}
        self._make_monitor = make_monitor
        # The EndpointRegistry, kept in step with the buckets' endpoints.
        self._endpoints = endpoints
        # Called after a change: the scheduler looks at the new buckets at once.
        self.on_change = on_change

    def __iter__(self) -> Iterator:
        with self._lock:
            return iter(list(self._monitors.values()))

    def __len__(self) -> int:
        with self._lock:
            return len(self._monitors)

    def get(self, key: str):
        with self._lock:
            return self._monitors.get(key)

    def endpoints(self) -> List[str]:
        return [m.endpoint for m in self if m.endpoint]

    def apply(self, specs: List[BucketSpec]) -> FleetChange:
        """Watch exactly these buckets, touching only those that changed.

        A bucket whose entry is unchanged keeps its monitor, its figures and
        its series. One that changed in any way is stopped and started afresh:
        a new detection and a new collection, simple and never half-applied.
        """
        change = FleetChange()
        current = {key: m for key, m in self._snapshot().items()}
        wanted = {spec.key: spec for spec in specs}

        started = {}
        for key, spec in wanted.items():
            old = current.get(key)
            if old is not None and old.spec.fingerprint() == spec.fingerprint():
                change.unchanged += 1
                continue
            (change.changed if old is not None else change.added).append(key)
            # Built and given its credentials before it is swapped in, outside
            # the lock: reading a hundred Secrets must not hold up a scrape.
            monitor = self._make_monitor(spec)
            monitor.refresh_credentials()
            started[key] = monitor
        change.removed = sorted(set(current) - set(wanted))

        with self._lock:
            for key in change.removed:
                del self._monitors[key]
            self._monitors.update(started)
            # The configured order, so the metrics and /readyz read like the file.
            self._monitors = {
                key: self._monitors[key] for key in wanted if key in self._monitors
            }

        for key in change.removed + change.changed:
            current[key].stop()

        for key in change.added:
            logging.info("Bucket %s added", key)
        for key in change.changed:
            logging.info("Bucket %s changed: watching it afresh", key)
        for key in change.removed:
            logging.info("Bucket %s removed", key)

        if self._endpoints is not None:
            self._endpoints.sync(self.endpoints())
        if change and self.on_change is not None:
            self.on_change()
        return change

    def _snapshot(self) -> Dict[str, object]:
        with self._lock:
            return dict(self._monitors)

    def stop(self):
        for monitor in self:
            monitor.stop()
