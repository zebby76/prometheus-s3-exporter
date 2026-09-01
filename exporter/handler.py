import json
import logging
import threading
import time
import uuid
from datetime import datetime
from wsgiref import simple_server

import falcon
from prometheus_client.exposition import choose_encoder
from prometheus_client.registry import REGISTRY

# Access lines go to their own logger: they are logfmt records meant for a
# parser, so they must not carry the human-readable prefix the rest of the
# process uses.
ACCESS_LOGGER_NAME = "access"

# Mirrors how the edge classifies traffic, which is why the health endpoints
# carry the `z` suffix: probe traffic is not user traffic and is filtered on
# this key rather than on the path.
_TRAFFIC_CLASSES = {
    "/metrics": "metrics",
    "/healthz": "health",
    "/readyz": "health",
}

# The correlation fields come from headers the reverse proxy stamps, and every
# edge names them differently. Keyed by the log field they fill, so a deployment
# renames what it has to and leaves the rest alone. Three of the four defaults
# are the real standard headers; only a transaction id has no standard name.
DEFAULT_PROXY_HEADERS = {
    "via": "Via",
    "vxid": "X-Transaction-Id",
    "xff": "X-Forwarded-For",
    "proto": "X-Forwarded-Proto",
}

# Apache renders a missing header or note as a bare dash; keep that so both
# tiers can be read by one parser without special cases.
_ABSENT = "-"


def _quote(value):
    """Escape a value going into a quoted logfmt field."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _access_logger():
    """Bare-message logger for access lines, configured once."""
    logger = logging.getLogger(ACCESS_LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        # Never reaches the root handler, whose format would prefix the line
        # with a second timestamp and break the logfmt parse.
        logger.propagate = False
        logger.setLevel(logging.INFO)
    return logger


def set_proxy_headers(headers):
    """Name the proxy headers the access log reads, by the field they fill.

    Merged onto the defaults rather than replacing them, so naming one header
    does not silently blank the other three.
    """
    resolved = dict(DEFAULT_PROXY_HEADERS)
    if isinstance(headers, dict):
        for field, name in headers.items():
            if field in resolved and name:
                resolved[field] = str(name)
    elif headers:
        logging.error("proxy_headers is not a mapping, using defaults: %r", headers)

    LogfmtRequestHandler.proxy_headers = resolved
    logging.info(
        "Access log correlation headers: %s",
        ", ".join(f"{field}={name}" for field, name in resolved.items()),
    )


class LogfmtRequestHandler(simple_server.WSGIRequestHandler):
    """Access logs in the same logfmt shape the Apache tier emits.

    wsgiref writes Common Log Format straight to stderr, bypassing `logging`
    altogether. Emitting the edge's key set instead means one parser reads both
    tiers, and a request can be followed across them on `vxid` and `via`.
    """

    # Set once at startup by set_proxy_headers(); a class attribute because
    # wsgiref instantiates the handler itself, once per request.
    proxy_headers = DEFAULT_PROXY_HEADERS

    def get_environ(self):
        # Called once per request, immediately before the app runs, so it
        # measures the application and the response write -- not the wait for
        # a client that has not sent its request line yet.
        self._started = time.perf_counter()
        self._rid = self._request_id()
        return super().get_environ()

    def _request_id(self):
        """Correlate with the edge when it sent an id, otherwise mint one."""
        return self.headers.get("X-Request-Id") or uuid.uuid4().hex[:16]

    def _header(self, name):
        return self.headers.get(name) or _ABSENT

    @staticmethod
    def _timestamp():
        now = datetime.now().astimezone()
        return "{}.{:03d}{}".format(
            now.strftime("%Y-%m-%dT%H:%M:%S"),
            now.microsecond // 1000,
            now.strftime("%z"),
        )

    def log_request(self, code="-", size="-"):
        elapsed = time.perf_counter() - getattr(self, "_started", time.perf_counter())
        target, _, query = self.path.partition("?")
        proxy = self.proxy_headers

        fields = [
            ("ts", self._timestamp(), False),
            ("rid", getattr(self, "_rid", _ABSENT), False),
            ("class", _TRAFFIC_CLASSES.get(target, "other"), False),
            ("src", "exporter", False),
            ("status", code, False),
            ("dur_us", int(elapsed * 1_000_000), False),
            ("method", self.command or _ABSENT, False),
            ("uri", self.path or _ABSENT, True),
            ("qs", query or _ABSENT, True),
            ("target", target or _ABSENT, True),
            # Apache's %B is 0 for an empty body, where wsgiref passes "-".
            ("bytes", 0 if size == "-" else size, False),
            ("host", self._header("Host"), False),
            ("via", self._header(proxy["via"]), False),
            ("vxid", self._header(proxy["vxid"]), False),
            ("xff", self._header(proxy["xff"]), True),
            ("proto", self._header(proxy["proto"]), False),
            ("ua", self._header("User-agent"), True),
        ]

        _access_logger().info(
            "%s",
            " ".join(
                '{}="{}"'.format(key, _quote(value)) if quoted else f"{key}={value}"
                for key, value, quoted in fields
            ),
        )

    def log_message(self, fmt, *args):
        # Malformed requests and the like: not access records, so they keep the
        # process-wide format rather than pretending to be logfmt.
        logging.warning("http %s - %s", self.address_string(), fmt % args)


class HealthMetricsHandler:
    """Liveness: is the process answering.

    Deliberately independent of S3. This drives the container HEALTHCHECK and
    any liveness probe, and restarting the pod does not fix an unreachable
    bucket -- it just throws away the metrics right when they are needed.
    """

    def on_get(self, _req, resp):
        resp.set_header("Content-Type", "application/json")
        resp.text = '{"status": true}'


class ReadinessHandler:
    """Readiness: can the bucket be reached, per the connectivity probe."""

    def __init__(self, probe):
        self._probe = probe

    def on_get(self, _req, resp):
        resp.set_header("Content-Type", "application/json")
        result = self._probe.result

        if result.checked_at is None:
            # Not probed yet: nothing to report against, so do not hold the
            # pod out of service on the strength of no evidence.
            resp.text = '{"status": true, "bucket": "unknown"}'
            return

        if result.reachable:
            resp.text = '{"status": true, "bucket": "reachable"}'
            return

        resp.status = falcon.HTTP_503
        resp.text = json.dumps(
            {
                "status": False,
                "bucket": "unreachable",
                "error": result.error,
                "age_seconds": round(result.age_seconds(), 1),
            }
        )


class CustomMetricsHandler:

    registry = REGISTRY

    def send_error(self, resp, status, message):
        resp.status = status
        resp.text = message

    def on_get(self, req, resp):
        registry = self.registry
        # req.headers upper-cases header names, so a "Accept" lookup there always
        # misses and content negotiation silently degrades to the legacy format.
        encoder, content_type = choose_encoder(req.get_header("Accept"))
        resp.set_header("Content-Type", content_type)
        try:
            output = encoder(
                registry
            )  # Todo: filter metrics to return only wcmtech metrics
        except Exception:
            self.send_error(
                resp, 500, "error generating metric output"
            )  # pylint: disable=no-member
            raise
        resp.text = output


def exporter(port=9773, addr="0.0.0.0"):

    global LOGGER

    logging.basicConfig(level=logging.DEBUG)
    LOGGER = logging.getLogger(__name__)

    api = falcon.App()
    api.add_route("/metrics", CustomMetricsHandler())

    _access_logger()
    httpd = simple_server.make_server(
        addr, port, api, handler_class=LogfmtRequestHandler
    )
    t = threading.Thread(target=httpd.serve_forever)
    t.daemon = True
    LOGGER.debug("Start Prometheus Exporter Listener on %s:%s/metrics ", addr, port)
    t.start()


def health(port=9774, addr="0.0.0.0", probe=None):

    global LOGGER

    logging.basicConfig(level=logging.DEBUG)
    LOGGER = logging.getLogger(__name__)

    api = falcon.App()
    api.add_route("/healthz", HealthMetricsHandler())
    # Only routed when a probe exists: without one there is nothing to base a
    # readiness answer on.
    if probe is not None:
        api.add_route("/readyz", ReadinessHandler(probe))

    _access_logger()
    httpd = simple_server.make_server(
        addr, port, api, handler_class=LogfmtRequestHandler
    )
    t = threading.Thread(target=httpd.serve_forever)
    t.daemon = True
    LOGGER.debug(
        "Start Prometheus Exporter Health Listener on %s:%s/healthz ", addr, port
    )
    t.start()
