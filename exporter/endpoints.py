"""Where along the way an endpoint breaks: DNS, then TCP, then TLS.

The bucket probes answer "is the bucket there"; this answers "where does the
path to the storage break", once per endpoint rather than once per bucket. A
hundred buckets on one grid share one name, one address and one certificate:
checking them a hundred times over says nothing more and costs a hundred
resolutions every cycle.

Each cycle resolves the endpoint's name, opens a TCP connection and, for an
`https` endpoint, completes a TLS handshake, timing each phase -- the way the
blackbox exporter does. It then closes the connection without sending a
request. The bucket probes keep their connection open between checks, so a
certificate that expired or a balancer that stopped answering TLS stays
invisible to them until they reconnect; this sees it within one cycle.
"""

from __future__ import annotations

import functools
import logging
import os
import random
import socket
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple

PHASES = ("dns", "tcp", "tls")

# A certificate closer than this to its end is worth a warning in the log.
CERT_WARNING_SECONDS = 14 * 24 * 3600
# ...repeated at most this often, not on every cycle.
CERT_WARNING_REPEAT_SECONDS = 24 * 3600

# How long the close_notify exchange may take once a check is done.
_CLOSE_NOTIFY_SECONDS = 1


@functools.lru_cache(maxsize=None)
def aws_endpoint(region: str) -> Optional[str]:
    """The URL boto3 sends S3 requests to without an endpoint_url.

    Computed by botocore itself rather than spelled out here, so partitions and
    FIPS or dual-stack settings come out as the S3 client sees them. With no
    endpoint_url botocore addresses buckets virtual-host style -- the requests
    go to `<bucket>.s3.<region>.amazonaws.com` -- so this checks the service's
    own name: the same infrastructure, not the same DNS record.
    """
    try:
        import boto3
        from botocore import UNSIGNED
        from botocore.config import Config

        client = boto3.client(
            "s3",
            region_name=region or None,
            config=Config(signature_version=UNSIGNED),
        )
        return client.meta.endpoint_url
    except Exception as exc:
        logging.error("Cannot work out the S3 endpoint of region %r: %s", region, exc)
        return None


def endpoint_id(endpoint_url: Optional[str], region: str = "") -> Optional[str]:
    """`scheme://host:port`, the identity buckets are grouped by; None if unusable.

    Lower-cased and with the port spelled out, so `https://Grid` and
    `https://grid:443/` are one endpoint.
    """
    url = endpoint_url or aws_endpoint(region)
    if not url:
        return None
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https") or not parsed.hostname:
        return None
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{scheme}://{host}:{port}"


def _split(endpoint: str) -> Tuple[str, str, int]:
    parsed = urllib.parse.urlsplit(endpoint)
    return parsed.scheme, parsed.hostname, parsed.port


@dataclass(frozen=True)
class EndpointResult:
    """Outcome of one cycle; frozen and replaced wholesale, like ProbeResult.

    A phase that was not reached keeps the duration it had on the last cycle
    that reached it: a resolution that failed says nothing new about TCP, and
    a zero would read as an instant connection.
    """

    up: bool = False
    checked_at: Optional[datetime] = None
    # The phase that failed, and why; None when every phase passed.
    failed_phase: Optional[str] = None
    error: Optional[str] = None
    addresses: Tuple[str, ...] = ()
    dns_seconds: Optional[float] = None
    tcp_seconds: Optional[float] = None
    tls_seconds: Optional[float] = None
    # Epoch seconds of the certificate's notAfter.
    cert_not_after: Optional[float] = None

    def age_seconds(self, now: Optional[datetime] = None) -> float:
        if self.checked_at is None:
            return 0.0
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - self.checked_at).total_seconds())


class _PhaseFailed(Exception):
    def __init__(self, phase: str, seconds: float, exc: BaseException):
        super().__init__(phase)
        self.phase = phase
        self.seconds = seconds
        self.exc = exc


def _connect(infos, timeout: float) -> socket.socket:
    """Connect to the resolved addresses in order, as urllib3 does."""
    last_error: Optional[OSError] = None
    for family, kind, proto, _, address in infos:
        sock = socket.socket(family, kind, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(address)
            return sock
        except OSError as exc:
            sock.close()
            last_error = exc
    raise last_error or OSError("no address to connect to")


class EndpointChecker:
    """Checks one endpoint, phase by phase, on its own thread."""

    def __init__(
        self,
        endpoint: str,
        interval_seconds: float,
        timeout_seconds: float,
        ca_bundle: Optional[str] = None,
        resolve=socket.getaddrinfo,
        connect=_connect,
        tls_context: Optional[Callable[[], ssl.SSLContext]] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.endpoint = endpoint
        self.scheme, self.host, self.port = _split(endpoint)
        self.tls = self.scheme == "https"
        self.interval_seconds = interval_seconds
        self.timeout_seconds = timeout_seconds
        self._ca_bundle = ca_bundle
        self._resolve = resolve
        self._connect = connect
        self._tls_context_factory = tls_context or self._default_tls_context
        self._tls_context: Optional[ssl.SSLContext] = None
        self._clock = clock
        self._result = EndpointResult()
        # Monotonic, one per phase this endpoint has.
        self._failures_total = {phase: 0 for phase in self.phases}
        self._cert_warned_at: Optional[float] = None

    @property
    def phases(self) -> Tuple[str, ...]:
        return PHASES if self.tls else PHASES[:2]

    @property
    def result(self) -> EndpointResult:
        return self._result

    @property
    def failures_total(self) -> Dict[str, int]:
        return dict(self._failures_total)

    def _default_tls_context(self) -> ssl.SSLContext:
        # The S3 client's trust: AWS_CA_BUNDLE when set, the system's otherwise.
        return ssl.create_default_context(cafile=self._ca_bundle or None)

    def _phase(self, name, fn, timings):
        started = time.perf_counter()
        try:
            value = fn()
        except Exception as exc:
            raise _PhaseFailed(name, time.perf_counter() - started, exc) from None
        timings[name] = time.perf_counter() - started
        return value

    def _context(self) -> ssl.SSLContext:
        """The TLS context, built once, before the handshake is timed.

        Loading the CAs takes tens of milliseconds, which the first handshake
        would otherwise report as its own. A bad CA bundle fails the TLS phase
        on every cycle until it is fixed, without a restart.
        """
        if self._tls_context is None:
            try:
                self._tls_context = self._tls_context_factory()
            except Exception as exc:
                raise _PhaseFailed("tls", 0.0, exc) from None
        return self._tls_context

    def _handshake(self, context: ssl.SSLContext, sock) -> ssl.SSLSocket:
        sock.settimeout(self.timeout_seconds)
        # Handshakes on the way in, and closes the socket if that fails.
        return context.wrap_socket(sock, server_hostname=self.host)

    @staticmethod
    def _close(sock):
        """Close, with a close_notify first on a TLS socket.

        A bare close shows in the balancer's log as an error on every cycle
        (EPIPE on HAProxy); a client that is done says so. Outside the timed
        phase, and briefly: a server that never answers it must not hold the
        thread.
        """
        if hasattr(sock, "unwrap"):
            try:
                sock.settimeout(_CLOSE_NOTIFY_SECONDS)
                sock.unwrap()
            except (OSError, ValueError):
                pass
        try:
            sock.close()
        except OSError:
            pass

    def check_once(self) -> EndpointResult:
        """Run one cycle and publish it. Never raises."""
        timings: Dict[str, float] = {}
        previous = self._result
        addresses = previous.addresses
        cert_not_after = previous.cert_not_after
        failed_phase = error = None
        sock = None
        try:
            infos = self._phase(
                "dns",
                lambda: self._resolve(self.host, self.port, type=socket.SOCK_STREAM),
                timings,
            )
            addresses = tuple(sorted({info[4][0] for info in infos}))
            sock = self._phase(
                "tcp", lambda: self._connect(infos, self.timeout_seconds), timings
            )
            if self.tls:
                context = self._context()
                # wrap_socket takes the socket over, and closes it on failure.
                handshake_of, sock = sock, None
                sock = self._phase(
                    "tls", lambda: self._handshake(context, handshake_of), timings
                )
                not_after = (sock.getpeercert() or {}).get("notAfter")
                if not_after:
                    cert_not_after = ssl.cert_time_to_seconds(not_after)
        except _PhaseFailed as failed:
            failed_phase = failed.phase
            error = f"{type(failed.exc).__name__}: {failed.exc}"
            timings[failed.phase] = failed.seconds
            self._failures_total[failed.phase] += 1
        finally:
            if sock is not None:
                self._close(sock)

        result = replace(
            previous,
            up=failed_phase is None,
            checked_at=datetime.now(timezone.utc),
            failed_phase=failed_phase,
            error=error,
            addresses=addresses,
            cert_not_after=cert_not_after,
            **{f"{phase}_seconds": seconds for phase, seconds in timings.items()},
        )
        self._result = result
        self._log(previous, result, timings)
        if result.up:
            self._check_certificate(result)
        return result

    def _summary(self, timings) -> str:
        return ", ".join(
            f"{phase} {seconds:.3f}s" for phase, seconds in timings.items()
        )

    def _log(self, previous: EndpointResult, result: EndpointResult, timings):
        if not result.up:
            # Same rhythm as the probes: the transition loud, repeats quiet.
            logging.log(
                (
                    logging.ERROR
                    if previous.up or previous.checked_at is None
                    else logging.DEBUG
                ),
                "Endpoint %s failed at %s after %.3fs: %s",
                self.endpoint,
                result.failed_phase,
                timings.get(result.failed_phase, 0.0),
                result.error,
            )
            return
        summary = self._summary(timings)
        if previous.checked_at is None:
            logging.info(
                "Endpoint %s reachable at %s (%s)",
                self.endpoint,
                ", ".join(result.addresses),
                summary,
            )
        elif not previous.up:
            logging.info("Endpoint %s is reachable again (%s)", self.endpoint, summary)
        elif result.addresses != previous.addresses:
            logging.info(
                "%s now resolves to %s (was %s)",
                self.host,
                ", ".join(result.addresses),
                ", ".join(previous.addresses),
            )
        else:
            logging.debug("Endpoint %s checked (%s)", self.endpoint, summary)

    def _check_certificate(self, result: EndpointResult):
        if result.cert_not_after is None:
            return
        now = self._clock()
        left = result.cert_not_after - now
        if left >= CERT_WARNING_SECONDS:
            return
        if (
            self._cert_warned_at is not None
            and now - self._cert_warned_at < CERT_WARNING_REPEAT_SECONDS
        ):
            return
        self._cert_warned_at = now
        logging.warning(
            "The certificate of %s expires in %.1f days, on %s",
            self.endpoint,
            left / 86400,
            datetime.fromtimestamp(result.cert_not_after, timezone.utc).isoformat(),
        )

    # -- what it shows ----------------------------------------------------------

    def rows(self) -> List[list]:
        labels = {"endpoint": self.endpoint}
        result = self._result
        rows = [
            [
                ["webtech_s3", "endpoint_failures_total"],
                {**labels, "phase": phase},
                float(count),
                "Endpoint checks that failed since start, by the phase that "
                "failed: dns, tcp or tls.",
            ]
            for phase, count in self._failures_total.items()
        ]
        if result.checked_at is None:
            return rows

        rows += [
            [
                ["webtech_s3", "endpoint_up"],
                labels,
                int(result.up),
                "1 when the last check of the endpoint passed every phase.",
            ],
            [
                ["webtech_s3", "endpoint_check_age_seconds"],
                labels,
                result.age_seconds(),
                "Age of the last check of the endpoint, in seconds.",
            ],
            [
                ["webtech_s3", "endpoint_addresses"],
                labels,
                len(result.addresses),
                "Addresses the endpoint's name resolved to.",
            ],
        ]
        durations = (
            ("dns", "endpoint_dns_duration_seconds", "Duration of the resolution"),
            ("tcp", "endpoint_tcp_connect_seconds", "Duration of the TCP connect"),
            ("tls", "endpoint_tls_handshake_seconds", "Duration of the TLS handshake"),
        )
        for phase, name, text in durations:
            seconds = getattr(result, f"{phase}_seconds")
            if phase in self.phases and seconds is not None:
                rows.append(
                    [
                        ["webtech_s3", name],
                        labels,
                        seconds,
                        f"{text} of the endpoint's last check that reached it, "
                        "in seconds.",
                    ]
                )
        if result.cert_not_after is not None:
            rows.append(
                [
                    ["webtech_s3", "endpoint_tls_cert_expiry_timestamp_seconds"],
                    labels,
                    result.cert_not_after,
                    "When the endpoint's certificate expires, in Unix time.",
                ]
            )
        return rows

    def readiness(self) -> dict:
        result = self._result
        if result.checked_at is None:
            return {"state": "unknown"}
        if result.up:
            return {"state": "up"}
        return {
            "state": "down",
            "phase": result.failed_phase,
            "error": result.error,
            "age_seconds": round(result.age_seconds(), 1),
        }

    # -- its thread -------------------------------------------------------------

    def run_until(self, stop: threading.Event, initial_delay: float = 0):
        if initial_delay and stop.wait(initial_delay):
            return
        while not stop.is_set():
            self.check_once()
            stop.wait(self.interval_seconds)

    def start(self, stop: threading.Event, initial_delay: float = 0):
        thread = threading.Thread(
            target=self.run_until,
            args=(stop, initial_delay),
            name=f"endpoint-{self.host}:{self.port}",
            daemon=True,
        )
        thread.start()
        logging.info(
            "Checking endpoint %s every %ss (%s)",
            self.endpoint,
            self.interval_seconds,
            " → ".join(self.phases),
        )
        return thread


class EndpointRegistry:
    """One checker per distinct endpoint, started and stopped as buckets come and go."""

    def __init__(
        self,
        interval_seconds: float,
        timeout_seconds: float,
        ca_bundle: Optional[str] = None,
        spread: bool = False,
        factory=EndpointChecker,
        rng: Callable[[], float] = random.random,
    ):
        self.interval_seconds = interval_seconds
        self.timeout_seconds = timeout_seconds
        self.ca_bundle = (
            ca_bundle if ca_bundle is not None else os.getenv("AWS_CA_BUNDLE")
        )
        self.spread = spread
        self._factory = factory
        self._rng = rng
        self._lock = threading.Lock()
        self._checkers: Dict[str, EndpointChecker] = {}
        self._stops: Dict[str, threading.Event] = {}

    @property
    def checkers(self) -> Dict[str, EndpointChecker]:
        with self._lock:
            return dict(self._checkers)

    def sync(self, endpoints: Iterable[Optional[str]]):
        """Check exactly these endpoints: start the new ones, stop the rest."""
        wanted = {endpoint for endpoint in endpoints if endpoint}
        with self._lock:
            for endpoint in sorted(set(self._checkers) - wanted):
                self._stops.pop(endpoint).set()
                del self._checkers[endpoint]
                logging.info("Endpoint %s no longer checked", endpoint)
            for endpoint in sorted(wanted - set(self._checkers)):
                checker = self._factory(
                    endpoint,
                    self.interval_seconds,
                    self.timeout_seconds,
                    ca_bundle=self.ca_bundle,
                )
                stop = threading.Event()
                checker.start(
                    stop, self._rng() * self.interval_seconds if self.spread else 0
                )
                self._checkers[endpoint] = checker
                self._stops[endpoint] = stop

    def stop(self):
        with self._lock:
            for stop in self._stops.values():
                stop.set()

    def rows(self) -> List[list]:
        rows = []
        for checker in self.checkers.values():
            rows += checker.rows()
        return rows

    def readiness(self) -> Dict[str, dict]:
        return {
            endpoint: checker.readiness() for endpoint, checker in self.checkers.items()
        }
