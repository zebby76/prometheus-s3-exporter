"""Just enough of a Kubernetes API server for the multi-bucket exporter.

One route, `GET /api/v1/namespaces/{ns}/secrets/{name}`, over HTTPS, answered
the way the real API server answers it:

- 401 without the ServiceAccount's Bearer token;
- 403 for a Secret the ServiceAccount has no Role for -- a missing RoleBinding;
- 404 for a Secret that does not exist;
- 200 with the Secret, its values base64-encoded.

The Secrets and the grants are read from the state directory on every request,
so `make obs/kube/*` changes them while the exporter runs. Standard library
only: it runs on the plain python image, like dnsfault. Logs the path and the
status of each request, never a value.
"""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATE = Path(os.environ.get("KUBE_STATE", "/state"))
CERT = os.environ.get("KUBE_CERT", "/tls/kube-api.pem")
PORT = int(os.environ.get("KUBE_PORT", "6443"))

_ROUTE = re.compile(r"^/api/v1/namespaces/([^/]+)/secrets/([^/?]+)$")


def _status(code: int, reason: str, message: str) -> dict:
    """A Kubernetes Status object, as the API server sends on errors."""
    return {
        "kind": "Status",
        "apiVersion": "v1",
        "status": "Failure",
        "message": message,
        "reason": reason,
        "code": code,
    }


def answer(state: Path, path: str, authorization: str):
    """(status, body) for one request, from the state directory."""
    token = (state / "sa" / "token").read_text(encoding="utf-8").strip()
    if authorization != f"Bearer {token}":
        return 401, _status(401, "Unauthorized", "Unauthorized")

    match = _ROUTE.match(path)
    if not match:
        return 404, _status(404, "NotFound", "the server could not find the resource")
    namespace, name = match.groups()
    ref = f"{namespace}/{name}"

    granted = json.loads((state / "rbac.json").read_text(encoding="utf-8"))
    if ref not in granted.get("get_secrets", []):
        return 403, _status(
            403,
            "Forbidden",
            f'secrets "{name}" is forbidden: User '
            '"system:serviceaccount:s3-exporter:s3-exporter" cannot get resource '
            f'"secrets" in API group "" in the namespace "{namespace}"',
        )

    secrets = json.loads((state / "secrets.json").read_text(encoding="utf-8"))
    if ref not in secrets:
        return 404, _status(404, "NotFound", f'secrets "{name}" not found')

    data = {
        key: base64.b64encode(value.encode("utf-8")).decode("ascii")
        for key, value in secrets[ref].items()
    }
    return 200, {
        "kind": "Secret",
        "apiVersion": "v1",
        "metadata": {"name": name, "namespace": namespace},
        "type": "Opaque",
        "data": data,
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 -- the http.server naming
        try:
            code, body = answer(STATE, self.path, self.headers.get("Authorization", ""))
        except (OSError, ValueError) as exc:
            code, body = 500, _status(500, "InternalError", type(exc).__name__)
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        # The request line and the status: never a header, never a body.
        sys.stdout.write(f"{self.address_string()} {fmt % args}\n")
        sys.stdout.flush()


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(CERT)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print(f"fake Kubernetes API on :{PORT}, state in {STATE}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
