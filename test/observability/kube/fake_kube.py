"""Just enough of a Kubernetes API server for the multi-bucket exporter.

Two routes over HTTPS, answered the way the real API server answers them:

- `GET /api/v1/namespaces/{ns}/secrets/{name}`: 403 for a Secret the
  ServiceAccount has no Role for -- a missing RoleBinding --, 404 for one that
  does not exist, 200 with the Secret, its values base64-encoded;
- `GET /api/v1/namespaces/{ns}/secrets?labelSelector=…`: the `list`, 403
  without a Role allowing it in that namespace, 400 for a selector that does not
  parse, 200 with the matching Secrets -- values included, as the real one
  does --, a page at a time when `limit` asks for it.

Both answer 401 without the ServiceAccount's Bearer token.

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
from urllib.parse import parse_qs, urlsplit

STATE = Path(os.environ.get("KUBE_STATE", "/state"))
CERT = os.environ.get("KUBE_CERT", "/tls/kube-api.pem")
PORT = int(os.environ.get("KUBE_PORT", "6443"))

_ROUTE = re.compile(r"^/api/v1/namespaces/([^/]+)/secrets/([^/?]+)$")
_LIST_ROUTE = re.compile(r"^/api/v1/namespaces/([^/]+)/secrets$")
_SA = "system:serviceaccount:s3-exporter:s3-exporter"

# A label key: an optional DNS-subdomain prefix and a name.
_LABEL_KEY = (
    r"(?:[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?/)?[A-Za-z0-9](?:[-A-Za-z0-9_.]*[A-Za-z0-9])?"
)
_SET = re.compile(rf"^({_LABEL_KEY})\s+(in|notin)\s+\(([^()]*)\)$")
_COMPARE = re.compile(rf"^({_LABEL_KEY})\s*(==|!=|=)\s*([-A-Za-z0-9_.]*)$")
_EXISTS = re.compile(rf"^(!?)({_LABEL_KEY})$")


def _requirements(selector: str):
    """The requirements of a label selector, as the API server parses them.

    ValueError for anything it would refuse.
    """
    parts, depth, current = [], 0, ""
    for char in selector:
        depth += {"(": 1, ")": -1}.get(char, 0)
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)

    requirements = []
    for part in (p.strip() for p in parts):
        if not part:
            if selector.strip():
                raise ValueError("found '', expected: identifier")
            continue
        if match := _SET.match(part):
            values = {v.strip() for v in match.group(3).split(",") if v.strip()}
            requirements.append((match.group(1), match.group(2), values))
        elif match := _COMPARE.match(part):
            op = "!=" if match.group(2) == "!=" else "="
            requirements.append((match.group(1), op, {match.group(3)}))
        elif match := _EXISTS.match(part):
            op = "!" if match.group(1) else "exists"
            requirements.append((match.group(2), op, set()))
        else:
            raise ValueError(f"unable to parse requirement: {part!r}")
    return requirements


def _matches(labels: dict, requirements) -> bool:
    for key, op, values in requirements:
        present = key in labels
        if op == "exists" and not present:
            return False
        if op == "!" and present:
            return False
        if op in ("=", "in") and labels.get(key) not in values:
            return False
        if op in ("!=", "notin") and present and labels[key] in values:
            return False
    return True


def _encoded(values: dict) -> dict:
    return {
        key: base64.b64encode(value.encode("utf-8")).decode("ascii")
        for key, value in values.items()
    }


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

    url = urlsplit(path)
    granted = json.loads((state / "rbac.json").read_text(encoding="utf-8"))
    secrets = json.loads((state / "secrets.json").read_text(encoding="utf-8"))

    match = _LIST_ROUTE.match(url.path)
    if match:
        return _list(match.group(1), parse_qs(url.query), granted, secrets)

    match = _ROUTE.match(url.path)
    if not match or url.query:
        return 404, _status(404, "NotFound", "the server could not find the resource")
    namespace, name = match.groups()
    ref = f"{namespace}/{name}"

    if ref not in granted.get("get_secrets", []):
        return 403, _status(
            403,
            "Forbidden",
            f'secrets "{name}" is forbidden: User "{_SA}" cannot get resource '
            f'"secrets" in API group "" in the namespace "{namespace}"',
        )
    if ref not in secrets:
        return 404, _status(404, "NotFound", f'secrets "{name}" not found')

    return 200, _secret(namespace, name, secrets[ref])


def _secret(namespace: str, name: str, secret: dict) -> dict:
    return {
        "kind": "Secret",
        "apiVersion": "v1",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": secret.get("labels", {}),
            "annotations": secret.get("annotations", {}),
        },
        "type": "Opaque",
        "data": _encoded(secret["data"]),
    }


def _list(namespace: str, query: dict, granted: dict, secrets: dict):
    if namespace not in granted.get("list_secrets", []):
        return 403, _status(
            403,
            "Forbidden",
            f'secrets is forbidden: User "{_SA}" cannot list resource "secrets" '
            f'in API group "" in the namespace "{namespace}"',
        )
    try:
        requirements = _requirements(query.get("labelSelector", [""])[0])
        limit = int(query.get("limit", ["0"])[0])
        offset = int(query.get("continue", ["0"])[0] or 0)
    except ValueError as exc:
        return 400, _status(400, "BadRequest", str(exc))

    items = [
        _secret(namespace, ref.split("/", 1)[1], secret)
        for ref, secret in sorted(secrets.items())
        if ref.startswith(f"{namespace}/")
        and _matches(secret.get("labels", {}), requirements)
    ]
    page = items[offset : offset + limit] if limit else items[offset:]
    following = offset + len(page)
    metadata = {"continue": str(following)} if following < len(items) else {}
    return 200, {
        "kind": "SecretList",
        "apiVersion": "v1",
        "metadata": metadata,
        "items": page,
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
