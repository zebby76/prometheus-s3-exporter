"""The local stack's fake Kubernetes API answers as the real one does.

Tested here so the stack never shows the exporter a cluster that cannot exist:
the status codes and body shape its client relies on, and a configuration the
exporter accepts.
"""

import base64
import importlib.util
import json
from pathlib import Path

import pytest

KUBE = Path(__file__).resolve().parents[1] / "observability" / "kube"


def load(name):
    spec = importlib.util.spec_from_file_location(f"stack_{name}", KUBE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fake_kube = load("fake_kube")


@pytest.fixture
def state(tmp_path):
    (tmp_path / "sa").mkdir()
    (tmp_path / "sa" / "token").write_text("t0ken\n")
    (tmp_path / "rbac.json").write_text(
        json.dumps({"get_secrets": ["app-a/app-a-s3", "app-a/missing-s3"]})
    )
    (tmp_path / "secrets.json").write_text(
        json.dumps({"app-a/app-a-s3": {"S3_KEY": "k", "S3_BUCKET": "uploads"}})
    )
    return tmp_path


PATH = "/api/v1/namespaces/app-a/secrets/app-a-s3"


def test_a_granted_secret_comes_base64_encoded(state):
    code, body = fake_kube.answer(state, PATH, "Bearer t0ken")

    assert code == 200
    assert body["kind"] == "Secret"
    assert base64.b64decode(body["data"]["S3_BUCKET"]) == b"uploads"


def test_no_token_is_a_401(state):
    assert fake_kube.answer(state, PATH, "Bearer wrong")[0] == 401


def test_a_secret_without_a_role_is_a_403_with_the_api_message(state):
    code, body = fake_kube.answer(
        state, "/api/v1/namespaces/app-b/secrets/app-b-s3", "Bearer t0ken"
    )

    assert code == 403
    assert body["kind"] == "Status"
    assert "cannot get resource" in body["message"]


def test_a_granted_but_missing_secret_is_a_404(state):
    path = "/api/v1/namespaces/app-a/secrets/missing-s3"
    assert fake_kube.answer(state, path, "Bearer t0ken")[0] == 404


def test_any_other_route_is_a_404(state):
    assert fake_kube.answer(state, "/api/v1/secrets", "Bearer t0ken")[0] == 404


def test_the_exporter_reads_it_like_the_real_api(state, monkeypatch):
    """The exporter's own client, against answers from the fake."""
    from exporter.kube import InClusterKubernetes, SecretForbidden

    class Response:
        def __init__(self, code, body):
            self.status_code = code
            self._body = body

        def json(self):
            return self._body

    class Session:
        def get(self, url, headers, verify, timeout):
            path = url.split(":6443", 1)[1]
            return Response(*fake_kube.answer(state, path, headers["Authorization"]))

    client = InClusterKubernetes("kube-api", "6443", state / "sa", session=Session())

    assert client.read_secret("app-a", "app-a-s3")["S3_BUCKET"] == "uploads"
    with pytest.raises(SecretForbidden):
        client.read_secret("app-b", "app-b-s3")


def test_the_stack_configuration_is_one_the_exporter_accepts():
    from exporter import parse_configuration
    from exporter.buckets import parse_buckets

    state = load("state")
    settings = parse_configuration(state.config_text(add_spare=True).encode())

    class Kube:
        def default_namespace(self):
            return "s3-exporter"

    environ = {"AWS_DEFAULT_REGION": "us-east-1", "OPENSHIFT_NAMESPACE": "demo"}
    specs = parse_buckets(settings, environ, Kube())

    assert len(specs) == len(state.SECRETS) + 1 + 1 + len(state.ENV_PREFIXES)
    probe_only = [spec.key for spec in specs if not spec.collect]
    assert probe_only == ["secret:app-b/app-b-exports-s3"]
