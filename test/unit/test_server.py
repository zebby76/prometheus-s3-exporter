"""The listeners keep answering whatever one client does.

Both used to be wsgiref's single-threaded server with no timeout: one client
that connected and sent nothing held every other request until it left.
"""

import socket
import time
import urllib.request

import pytest

from exporter import handler


@pytest.fixture
def health_server():
    server = handler.health(port=0, addr="127.0.0.1")
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def metrics_server():
    server = handler.exporter(port=0, addr="127.0.0.1")
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _get(server, path):
    url = f"http://127.0.0.1:{server.server_port}{path}"
    with urllib.request.urlopen(url, timeout=2) as response:
        return response.status


@pytest.mark.parametrize(
    "fixture, path", [("health_server", "/healthz"), ("metrics_server", "/metrics")]
)
def test_an_idle_connection_does_not_block_other_requests(request, fixture, path):
    server = request.getfixturevalue(fixture)
    idle = socket.create_connection(("127.0.0.1", server.server_port))
    try:
        # Several in a row: the single-threaded server timed every one out.
        for _ in range(3):
            assert _get(server, path) == 200
    finally:
        idle.close()


def test_a_silent_connection_is_dropped(health_server, monkeypatch):
    monkeypatch.setattr(handler.LogfmtRequestHandler, "timeout", 0.5)
    idle = socket.create_connection(("127.0.0.1", health_server.server_port))
    idle.settimeout(5)
    started = time.monotonic()
    try:
        # The server closes its end: recv returns b"" instead of blocking.
        assert idle.recv(1) == b""
    finally:
        idle.close()
    assert time.monotonic() - started < 4
