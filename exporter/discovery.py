"""Buckets found by a label selector instead of being named one by one.

An entry `{ selector: "s3-exporter/watch=true", namespace: app-a }` lists the
Secrets of app-a that carry the label, on a period of its own, and watches one
bucket per Secret found. The Secret's annotations, under a configurable prefix,
set what the configuration would have set for a named entry.

`list` returns the values with the names, so the values come from the list
itself and the Role needs `list` only. That is also the price: RBAC cannot
filter by label, and a Role that allows `list` on Secrets lets the exporter read
every Secret of that namespace. The selector sorts the answer; it does not limit
the access.

A list that fails changes nothing: the buckets already found stay watched with
the credentials in hand, and the failure shows in the discovery metrics. Only a
list that succeeds and no longer holds a Secret stops its bucket -- an API
outage must never empty the dashboards.

No value read from a Secret reaches a log line, a metric or an exception: the
changes are told apart with a digest kept in memory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from exporter.buckets import BucketPlan, BucketSpec, SelectorRule
from exporter.credentials import (
    CredentialsUnavailable,
    ListedSecretCredentials,
    UnusableCredentials,
)
from exporter.kube import (
    KubernetesError,
    ListedSecret,
    SecretForbidden,
    SelectorInvalid,
)
from exporter.sources import AUTO, SOURCES

DEFAULT_DISCOVERY_SECONDS = 60

# Why a list failed, for webtech_s3_discovery_failures_total.
REASONS = ("forbidden", "invalid", "unavailable")

# What a Secret may set about its own bucket, under `<prefix>/`.
ANNOTATIONS = (
    "interval",
    "collect",
    "endpoint-url",
    "region",
    "source",
    "bucket-name",
    "access-key-field",
    "secret-key-field",
    "bucket-field",
)
_KEY_FIELDS = {
    "access-key-field": "access_key",
    "secret-key-field": "secret_key",
    "bucket-field": "bucket",
}


def _reason(exc: KubernetesError) -> str:
    if isinstance(exc, SecretForbidden):
        return "forbidden"
    if isinstance(exc, SelectorInvalid):
        return "invalid"
    return "unavailable"


def _digest(data: Dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()


class SecretDiscovery:
    """The Secrets of one namespace that match one selector, as last listed."""

    def __init__(self, kube, namespace: str, selector: str, clock=time.time):
        self.kube = kube
        self.namespace = namespace
        self.selector = selector
        self._clock = clock
        self._lock = threading.Lock()
        self.secrets: Dict[str, ListedSecret] = {}
        self._digests: Dict[str, str] = {}
        # None until a list succeeds once.
        self.last_success: Optional[float] = None
        self.error: Optional[str] = None
        self.failures = {reason: 0 for reason in REASONS}

    @property
    def where(self) -> Tuple[str, str]:
        return (self.namespace, self.selector)

    def describe(self) -> str:
        return f"selector {self.selector!r} in {self.namespace}"

    @property
    def up(self) -> bool:
        return self.last_success is not None and self.error is None

    def refresh(self) -> Set[str]:
        """List again. The names already known whose values changed; on a
        failure, nothing: the last list stays in force."""
        try:
            found = self.kube.list_secrets(self.namespace, self.selector)
        except KubernetesError as exc:
            message = str(exc)
            with self._lock:
                first = self.error != message
                self.error = message
                self.failures[_reason(exc)] += 1
                kept = len(self.secrets)
            logging.log(
                logging.ERROR if first else logging.DEBUG,
                "Discovery by %s failed, keeping the %s secret(s) found before: %s",
                self.describe(),
                kept,
                message,
            )
            return set()

        digests = {name: _digest(secret.data) for name, secret in found.items()}
        with self._lock:
            recovered = self.error is not None
            first = self.last_success is None
            before = set(self.secrets)
            changed = {
                name
                for name, digest in digests.items()
                if name in self._digests and self._digests[name] != digest
            }
            self.secrets = found
            self._digests = digests
            self.last_success = self._clock()
            self.error = None

        if first or recovered or before != set(found):
            logging.info(
                "Discovery by %s: %s secret(s)%s",
                self.describe(),
                len(found),
                "".join(
                    f", {label} {', '.join(sorted(names))}"
                    for label, names in (
                        ("new", set(found) - before),
                        ("gone", before - set(found)),
                    )
                    if names and not first
                ),
            )
        return changed

    def values(self, name: str) -> Dict[str, str]:
        """The values of a listed Secret, for ListedSecretCredentials."""
        with self._lock:
            secret = self.secrets.get(name)
        if secret is None:
            raise CredentialsUnavailable(
                f"secret {self.namespace}/{name} is not found by {self.describe()}"
            )
        if secret.error:
            raise CredentialsUnavailable(secret.error)
        return secret.data

    def snapshot(self) -> Dict[str, ListedSecret]:
        with self._lock:
            return dict(self.secrets)

    def rows(self) -> List[list]:
        # bucket_namespace, as on the buckets it finds: `namespace` is the
        # scrape target's own on most Prometheus setups.
        labels = {"bucket_namespace": self.namespace, "selector": self.selector}
        with self._lock:
            rows = [
                [
                    ["webtech_s3", "discovery_failures_total"],
                    {**labels, "reason": reason},
                    float(count),
                    "Lists of a selector's Secrets that failed since start, by "
                    "reason: forbidden, invalid or unavailable.",
                ]
                for reason, count in self.failures.items()
            ]
            rows += [
                [
                    ["webtech_s3", "discovery_up"],
                    labels,
                    int(self.up),
                    "1 when the last list of the selector's Secrets succeeded.",
                ],
                [
                    ["webtech_s3", "discovery_secrets"],
                    labels,
                    len(self.secrets),
                    "Secrets the last successful list of the selector found.",
                ],
            ]
            if self.last_success is not None:
                rows.append(
                    [
                        ["webtech_s3", "discovery_last_success_timestamp_seconds"],
                        labels,
                        self.last_success,
                        "When the selector's Secrets were last listed, in Unix "
                        "time.",
                    ]
                )
        return rows

    def readiness(self) -> dict:
        with self._lock:
            return {
                "namespace": self.namespace,
                "selector": self.selector,
                "up": self.up,
                "secrets": len(self.secrets),
                "error": self.error,
            }


def _own_annotations(secret: ListedSecret, prefix: str) -> Dict[str, str]:
    head = f"{prefix}/"
    return {
        key[len(head) :]: value
        for key, value in secret.annotations.items()
        if key.startswith(head)
    }


def _settings_from(own: Dict[str, str], rule: SelectorRule, prefix: str):
    """The bucket's settings, the rule's overridden by the annotations.

    Raises ValueError with what to fix.
    """
    unknown = sorted(set(own) - set(ANNOTATIONS))
    if unknown:
        raise ValueError(
            f"unknown annotation(s) {', '.join(f'{prefix}/{k}' for k in unknown)} "
            f"(known: {', '.join(ANNOTATIONS)})"
        )
    for name, value in own.items():
        if not value.strip():
            raise ValueError(f"annotation {prefix}/{name} is empty")

    settings = {
        "keys": dict(rule.keys),
        "bucket_name": own.get("bucket-name"),
        "endpoint_url": own.get("endpoint-url", rule.endpoint_url),
        "region": own.get("region", rule.region),
        "interval_minutes": rule.interval_minutes,
        "source": rule.source,
        "collect": rule.collect,
    }
    for annotation, field_name in _KEY_FIELDS.items():
        if annotation in own:
            settings["keys"][field_name] = own[annotation]

    if "interval" in own:
        try:
            interval = float(own["interval"])
        except ValueError:
            interval = 0.0
        if not interval > 0:
            raise ValueError(
                f"annotation {prefix}/interval must be a number of minutes above "
                f"0, got {own['interval']!r}"
            )
        settings["interval_minutes"] = interval
    if "collect" in own:
        value = own["collect"].strip().lower()
        if value not in ("true", "false"):
            raise ValueError(
                f"annotation {prefix}/collect must be true or false, "
                f"got {own['collect']!r}"
            )
        settings["collect"] = value == "true"
    if "source" in own:
        known = [AUTO] + [candidate.name for candidate in SOURCES]
        value = own["source"].strip().lower()
        if value not in known:
            raise ValueError(
                f"annotation {prefix}/source must be one of {', '.join(known)}, "
                f"got {own['source']!r}"
            )
        settings["source"] = value
    if not settings["region"]:
        raise ValueError(f"no region (annotate {prefix}/region, or set one)")
    return settings


def discovered_spec(
    rule: SelectorRule,
    listing: SecretDiscovery,
    name: str,
    secret: ListedSecret,
    prefix: str,
) -> BucketSpec:
    """The bucket one listed Secret stands for; unusable, with the reason, when
    its annotations cannot be honoured."""
    key = f"secret:{rule.namespace}/{name}"
    try:
        settings = _settings_from(_own_annotations(secret, prefix), rule, prefix)
    except ValueError as exc:
        return BucketSpec(
            key=key,
            credentials=UnusableCredentials(rule.namespace, name, str(exc)),
            label_namespace=rule.namespace,
            endpoint_url=rule.endpoint_url,
            region=rule.region,
            interval_minutes=rule.interval_minutes,
            source=rule.source,
            collect=rule.collect,
        )
    return BucketSpec(
        key=key,
        credentials=ListedSecretCredentials(
            listing, name, settings["keys"], bucket_name=settings["bucket_name"]
        ),
        label_namespace=rule.namespace,
        endpoint_url=settings["endpoint_url"],
        region=settings["region"],
        interval_minutes=settings["interval_minutes"],
        source=settings["source"],
        collect=settings["collect"],
    )


class Discoverer:
    """Keeps the fleet equal to the named buckets plus the discovered ones.

    The reloader and the discovery both change the fleet; both go through here,
    under one lock, so neither drops the buckets the other one brought.
    """

    def __init__(
        self,
        kube,
        plan: BucketPlan,
        interval_seconds: float,
        stop: threading.Event,
        clock: Callable[[], float] = time.time,
    ):
        self.kube = kube
        self.interval_seconds = interval_seconds
        self._stop = stop
        self._clock = clock
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._listings: Dict[Tuple[str, str], SecretDiscovery] = {}
        self._overlaps_logged: Set[Tuple[str, str]] = set()
        self.plan = plan
        self.fleet = None
        self._sync_listings()

    # -- what is listed --------------------------------------------------------

    def _sync_listings(self) -> List[SecretDiscovery]:
        """One listing per rule, those of unchanged rules kept: their monitors
        read from them. Returns the new ones."""
        wanted = [rule.where() for rule in self.plan.rules]
        new = []
        for where in wanted:
            if where not in self._listings:
                listing = SecretDiscovery(self.kube, *where, clock=self._clock)
                self._listings[where] = listing
                new.append(listing)
        for where in set(self._listings) - set(wanted):
            del self._listings[where]
        return new

    def listings(self) -> List[SecretDiscovery]:
        with self._lock:
            return [self._listings[rule.where()] for rule in self.plan.rules]

    def refresh(self, listings: Optional[Iterable[SecretDiscovery]] = None):
        """List again; the bucket keys whose values changed."""
        changed = set()
        for listing in listings if listings is not None else self.listings():
            if self._stop.is_set():
                break
            changed |= {
                f"secret:{listing.namespace}/{name}" for name in listing.refresh()
            }
        return changed

    def desired(self) -> List[BucketSpec]:
        """The named buckets, then the discovered ones. A Secret also named
        explicitly keeps its explicit entry; one found by two selectors goes to
        the first in the file."""
        with self._lock:
            plan = self.plan
            specs = list(plan.specs)
            taken = {spec.key: "an explicit entry" for spec in specs}
            for rule in plan.rules:
                listing = self._listings[rule.where()]
                for name, secret in sorted(listing.snapshot().items()):
                    key = f"secret:{rule.namespace}/{name}"
                    if key in taken:
                        self._log_overlap(key, listing, taken[key])
                        continue
                    taken[key] = listing.describe()
                    specs.append(
                        discovered_spec(
                            rule, listing, name, secret, plan.annotation_prefix
                        )
                    )
            return specs

    def _log_overlap(self, key: str, listing: SecretDiscovery, owner: str):
        if (key, owner) in self._overlaps_logged:
            return
        self._overlaps_logged.add((key, owner))
        logging.info(
            "%s is also found by %s: %s keeps it", key, listing.describe(), owner
        )

    def discover(self) -> List[BucketSpec]:
        """List every selector once, and the buckets to watch: at startup and
        for --check-config, before any fleet exists."""
        with self._lock:
            self.refresh()
            return self.desired()

    # -- applying it ---------------------------------------------------------

    def run_once(self):
        """List every selector and bring the fleet in line."""
        with self._lock:
            changed = self.refresh()
            change = self.fleet.apply(self.desired())
        self._reread(changed)
        return change

    def _reread(self, keys: Set[str]):
        # A rotation seen by the list: the bucket takes the new key now, not at
        # the next credentials refresh.
        for key in sorted(keys):
            monitor = self.fleet.get(key)
            if monitor is not None and isinstance(
                monitor.spec.credentials, ListedSecretCredentials
            ):
                logging.info("%s changed: reading its credentials again", key)
                monitor.refresh_credentials()

    def set_plan(self, plan: BucketPlan):
        """A reloaded configuration: new selectors are listed at once, and the
        fleet follows. Returns what Fleet.apply did."""
        with self._lock:
            self.plan = plan
            self.refresh(self._sync_listings())
            return self.fleet.apply(self.desired())

    def relist_for(self, monitors: Iterable):
        """Before the refresher reads again the credentials S3 rejected: list
        the namespaces those Secrets came from, once each."""
        wanted = {
            monitor.spec.credentials.listing.where
            for monitor in monitors
            if isinstance(monitor.spec.credentials, ListedSecretCredentials)
        }
        if not wanted:
            return
        with self._lock:
            listings = [
                listing for listing in self.listings() if listing.where in wanted
            ]
            self.refresh(listings)
            self.fleet.apply(self.desired())

    # -- the thread ------------------------------------------------------------

    def request(self):
        """List now (SIGHUP)."""
        self._wake.set()

    def run(self):
        deadline = self._clock() + self.interval_seconds
        while not self._stop.is_set():
            # Short waits: a reload may turn the period on, off or change it.
            woken = self._wake.wait(1)
            if self._stop.is_set():
                return
            if woken:
                self._wake.clear()
            elif not self.interval_seconds or self._clock() < deadline:
                continue
            deadline = self._clock() + self.interval_seconds
            try:
                self.run_once()
            except Exception:
                logging.exception("Discovery failed")

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self.run, name="discovery", daemon=True)
        self._thread.start()
        return self._thread

    # -- what it shows -----------------------------------------------------------

    def rows(self) -> List[list]:
        rows = []
        for listing in self.listings():
            rows += listing.rows()
        return rows

    def readiness(self) -> Dict[str, dict]:
        return {
            f"selector:{listing.namespace}/{listing.selector}": listing.readiness()
            for listing in self.listings()
        }
