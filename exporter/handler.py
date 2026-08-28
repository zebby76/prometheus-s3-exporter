import json
import logging
import threading
from wsgiref import simple_server

import falcon
from prometheus_client.exposition import choose_encoder
from prometheus_client.registry import REGISTRY


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

    httpd = simple_server.make_server(addr, port, api)
    t = threading.Thread(target=httpd.serve_forever)
    t.daemon = True
    LOGGER.debug("Start Prometheus Exporter Listener on %s:%s/metrics ", addr, port)
    t.start()


def health(port=9774, addr="0.0.0.0", probe=None):

    global LOGGER

    logging.basicConfig(level=logging.DEBUG)
    LOGGER = logging.getLogger(__name__)

    api = falcon.App()
    api.add_route("/health", HealthMetricsHandler())
    # Only routed when a probe exists: without one there is nothing to base a
    # readiness answer on.
    if probe is not None:
        api.add_route("/ready", ReadinessHandler(probe))

    httpd = simple_server.make_server(addr, port, api)
    t = threading.Thread(target=httpd.serve_forever)
    t.daemon = True
    LOGGER.debug(
        "Start Prometheus Exporter Health Listener on %s:%s/health ", addr, port
    )
    t.start()
