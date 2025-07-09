import logging
import threading
from wsgiref import simple_server

import falcon
from prometheus_client.exposition import choose_encoder
from prometheus_client.registry import REGISTRY


class HealthMetricsHandler:
    def on_get(self, _req, resp):
        resp.set_header("Content-Type", "text/html")
        resp.text = '{"status": true}'


class CustomMetricsHandler:

    registry = REGISTRY

    def send_error(self, resp, status, message):
        resp.status = status
        resp.text = message

    def on_get(self, req, resp):
        registry = self.registry
        encoder, content_type = choose_encoder(req.headers.get("Accept"))
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


def health(port=9774, addr="0.0.0.0"):

    global LOGGER

    logging.basicConfig(level=logging.DEBUG)
    LOGGER = logging.getLogger(__name__)

    api = falcon.App()
    api.add_route("/health", HealthMetricsHandler())

    httpd = simple_server.make_server(addr, port, api)
    t = threading.Thread(target=httpd.serve_forever)
    t.daemon = True
    LOGGER.debug(
        "Start Prometheus Exporter Health Listener on %s:%s/health ", addr, port
    )
    t.start()
