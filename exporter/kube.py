"""Just enough of the Kubernetes API to read Secrets from inside a pod.

Two calls, authenticated with the pod's ServiceAccount: `GET` of one Secret by
name, and the `list` of a namespace's Secrets that match a label selector. The
official client would bring a dependency tree of its own for that; `requests`
is already here.

Nothing read from a Secret ever reaches a log line or an exception message:
errors carry the namespace, the name and the API's own `message`, never the
response body.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlencode

import requests

SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
TIMEOUT_SECONDS = 10
# Secrets per page of a `list`: the API server's own advice, and a bound on what
# one answer holds in memory.
LIST_PAGE_SIZE = 500

# What Kubernetes itself accepts: a namespace is a DNS label, a Secret name a DNS
# subdomain. Checked before either goes into the URL path, so a name holding a
# `/` or `..` cannot point the request at another API path.
_NAMESPACE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_SECRET_NAME = re.compile(r"^(?=.{1,253}$)[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")


def valid_namespace(value) -> bool:
    return isinstance(value, str) and bool(_NAMESPACE.match(value))


def valid_secret_name(value) -> bool:
    return isinstance(value, str) and bool(_SECRET_NAME.match(value))


class KubernetesError(Exception):
    """A Secret could not be read. The message never holds a secret value."""


class SecretForbidden(KubernetesError):
    """403: the ServiceAccount may not read this Secret."""


class SecretNotFound(KubernetesError):
    """404: no such Secret in that namespace."""


class KubernetesUnavailable(KubernetesError):
    """The API could not be reached or did not answer usably."""


class SelectorInvalid(KubernetesError):
    """400: the API server cannot parse the label selector."""


@dataclass(frozen=True)
class ListedSecret:
    """One Secret of a `list`: its keys decoded, and its annotations.

    `error` is set, and `data` empty, when its values cannot be decoded: one
    malformed Secret must not hide every other Secret of the namespace.
    """

    data: Dict[str, str] = field(repr=False)
    annotations: Dict[str, str]
    error: Optional[str] = None


def _decode(where: str, data) -> Dict[str, str]:
    """A Secret's `data`, base64-decoded. The messages name keys, never values."""
    if not isinstance(data, dict):
        raise KubernetesUnavailable(f"secret {where}: unexpected `data` field")
    decoded = {}
    for key, value in data.items():
        try:
            decoded[key] = base64.b64decode(value, validate=True).decode("utf-8")
        except (binascii.Error, TypeError, UnicodeDecodeError):
            # The key, never the value.
            raise KubernetesUnavailable(
                f"secret {where}: key {key} is not valid base64 text"
            ) from None
    return decoded


def _api_message(response) -> str:
    """The `message` of a Kubernetes Status, and nothing else of the body."""
    try:
        message = response.json().get("message")
    except (ValueError, AttributeError):
        return ""
    return str(message) if message else ""


class InClusterKubernetes:
    """The API server, as seen from a pod through its ServiceAccount."""

    def __init__(
        self,
        host: str,
        port: str,
        service_account_dir: Path = SERVICE_ACCOUNT_DIR,
        session=None,
    ):
        # An IPv6 service address has to be bracketed inside a URL.
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        self.base_url = f"https://{host}:{port}"
        self.token_path = service_account_dir / "token"
        self.ca_path = service_account_dir / "ca.crt"
        self.namespace_path = service_account_dir / "namespace"
        self._session = session or requests.Session()

    @classmethod
    def detect(
        cls, environ=None, service_account_dir: Path = SERVICE_ACCOUNT_DIR
    ) -> Optional["InClusterKubernetes"]:
        """A client when this process runs in a pod, else None."""
        environ = os.environ if environ is None else environ
        host = environ.get("KUBERNETES_SERVICE_HOST", "")
        port = environ.get("KUBERNETES_SERVICE_PORT", "")
        if not host or not port or not (service_account_dir / "token").is_file():
            return None
        return cls(host, port, service_account_dir)

    def default_namespace(self) -> Optional[str]:
        """The namespace this pod runs in, from its ServiceAccount."""
        try:
            return self.namespace_path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _token(self) -> str:
        # Read on every call: projected ServiceAccount tokens expire, about
        # hourly, and the kubelet rewrites the file. A token kept in memory
        # would turn every read into a 401 an hour after start.
        try:
            return self.token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise KubernetesUnavailable(
                f"cannot read the ServiceAccount token: {exc.strerror}"
            ) from None

    def read_secret(self, namespace: str, name: str) -> Dict[str, str]:
        """Every key of the Secret, decoded. Raises a KubernetesError."""
        if not valid_namespace(namespace) or not valid_secret_name(name):
            raise KubernetesError(f"invalid secret reference {namespace!r}/{name!r}")
        where = f"{namespace}/{name}"
        url = f"{self.base_url}/api/v1/namespaces/{namespace}/secrets/{name}"
        try:
            response = self._session.get(
                url,
                headers={"Authorization": f"Bearer {self._token()}"},
                verify=str(self.ca_path),
                timeout=TIMEOUT_SECONDS,
            )
        except requests.exceptions.SSLError:
            raise KubernetesUnavailable(
                f"TLS verification of the Kubernetes API failed for secret {where} "
                f"(is {self.ca_path} the cluster CA?)"
            ) from None
        except requests.RequestException as exc:
            raise KubernetesUnavailable(
                f"cannot reach the Kubernetes API for secret {where}: "
                f"{type(exc).__name__}"
            ) from None

        status = response.status_code
        if status == 403:
            raise SecretForbidden(
                f"reading secret {where} is forbidden "
                "(missing Role/RoleBinding for `get` on this secret?)"
            )
        if status == 404:
            raise SecretNotFound(f"secret {where} does not exist")
        if status != 200:
            detail = _api_message(response)
            raise KubernetesUnavailable(
                f"reading secret {where} returned HTTP {status}"
                + (f": {detail}" if detail else "")
            )

        try:
            data = response.json().get("data") or {}
        except (ValueError, AttributeError):
            raise KubernetesUnavailable(
                f"secret {where}: the API answer is not JSON"
            ) from None
        return _decode(where, data)

    def list_secrets(self, namespace: str, selector: str) -> Dict[str, ListedSecret]:
        """The namespace's Secrets that match `selector`, by name.

        Every page, following `continue`. `list` hands out the values as `get`
        does: the Role that allows it allows reading every Secret of the
        namespace, whatever the selector. Raises a KubernetesError.
        """
        if not valid_namespace(namespace):
            raise KubernetesError(f"invalid namespace {namespace!r}")
        where = f"in {namespace} matching {selector!r}"
        url = f"{self.base_url}/api/v1/namespaces/{namespace}/secrets"
        found: Dict[str, ListedSecret] = {}
        token = ""
        while True:
            query = {"labelSelector": selector, "limit": LIST_PAGE_SIZE}
            if token:
                query["continue"] = token
            page = self._list_page(f"{url}?{urlencode(query)}", namespace, where)
            for item in page.get("items") or []:
                metadata = (item or {}).get("metadata") or {}
                name = metadata.get("name")
                if not isinstance(name, str):
                    continue
                annotations = metadata.get("annotations") or {}
                if not isinstance(annotations, dict):
                    annotations = {}
                try:
                    data = _decode(f"{namespace}/{name}", item.get("data") or {})
                except KubernetesError as exc:
                    found[name] = ListedSecret({}, dict(annotations), str(exc))
                    continue
                found[name] = ListedSecret(data, dict(annotations))
            token = (page.get("metadata") or {}).get("continue") or ""
            if not token:
                return found

    def _list_page(self, url: str, namespace: str, where: str) -> dict:
        try:
            response = self._session.get(
                url,
                headers={"Authorization": f"Bearer {self._token()}"},
                verify=str(self.ca_path),
                timeout=TIMEOUT_SECONDS,
            )
        except requests.exceptions.SSLError:
            raise KubernetesUnavailable(
                f"TLS verification of the Kubernetes API failed listing secrets "
                f"{where} (is {self.ca_path} the cluster CA?)"
            ) from None
        except requests.RequestException as exc:
            raise KubernetesUnavailable(
                f"cannot reach the Kubernetes API listing secrets {where}: "
                f"{type(exc).__name__}"
            ) from None

        status = response.status_code
        if status == 403:
            raise SecretForbidden(
                f"listing secrets in {namespace} is forbidden "
                "(missing Role/RoleBinding for `list` on secrets?)"
            )
        if status == 400:
            detail = _api_message(response)
            raise SelectorInvalid(
                f"listing secrets {where}: the selector is invalid"
                + (f": {detail}" if detail else "")
            )
        if status != 200:
            detail = _api_message(response)
            raise KubernetesUnavailable(
                f"listing secrets {where} returned HTTP {status}"
                + (f": {detail}" if detail else "")
            )
        try:
            page = response.json()
        except ValueError:
            page = None
        if not isinstance(page, dict):
            raise KubernetesUnavailable(
                f"listing secrets {where}: the API answer is not a list"
            )
        return page
