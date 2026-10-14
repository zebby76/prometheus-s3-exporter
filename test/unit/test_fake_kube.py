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
        json.dumps(
            {
                "get_secrets": ["app-a/app-a-s3", "app-a/missing-s3"],
                "list_secrets": ["app-b"],
            }
        )
    )

    def secret(bucket, labels=None):
        return {
            "data": {"S3_KEY": "k", "S3_SECRET": "s", "S3_BUCKET": bucket},
            "labels": labels or {},
            "annotations": {"s3-exporter/interval": "5"},
        }

    watched = {"s3-exporter/watch": "true"}
    (tmp_path / "secrets.json").write_text(
        json.dumps(
            {
                "app-a/app-a-s3": secret("uploads"),
                "app-b/one-s3": secret("one", watched),
                "app-b/two-s3": secret("two", {**watched, "team": "x"}),
                "app-b/other-s3": secret("other"),
                "app-c/three-s3": secret("three", watched),
            }
        )
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


LIST = "/api/v1/namespaces/app-b/secrets"


def names(body):
    return [item["metadata"]["name"] for item in body["items"]]


def test_a_list_returns_the_matching_secrets_values_included(state):
    code, body = fake_kube.answer(
        state, f"{LIST}?labelSelector=s3-exporter%2Fwatch%3Dtrue", "Bearer t0ken"
    )

    assert code == 200
    assert body["kind"] == "SecretList"
    assert names(body) == ["one-s3", "two-s3"]
    assert base64.b64decode(body["items"][0]["data"]["S3_BUCKET"]) == b"one"
    assert body["items"][0]["metadata"]["annotations"] == {"s3-exporter/interval": "5"}


@pytest.mark.parametrize(
    "selector, expected",
    [
        ("", ["one-s3", "other-s3", "two-s3"]),
        ("s3-exporter/watch=true,team=x", ["two-s3"]),
        ("s3-exporter/watch,!team", ["one-s3"]),
        ("team in (x, y)", ["two-s3"]),
        ("team notin (x)", ["one-s3", "other-s3"]),
        ("s3-exporter/watch!=true", ["other-s3"]),
    ],
)
def test_the_selector_works_as_the_api_server_s(state, selector, expected):
    from urllib.parse import urlencode

    path = f"{LIST}?{urlencode({'labelSelector': selector})}"
    assert names(fake_kube.answer(state, path, "Bearer t0ken")[1]) == expected


def test_a_list_pages_with_limit_and_continue(state):
    code, first = fake_kube.answer(state, f"{LIST}?limit=2", "Bearer t0ken")
    token = first["metadata"]["continue"]
    _, second = fake_kube.answer(
        state, f"{LIST}?limit=2&continue={token}", "Bearer t0ken"
    )

    assert names(first) + names(second) == ["one-s3", "other-s3", "two-s3"]
    assert "continue" not in second["metadata"]


def test_a_list_without_the_role_is_a_403(state):
    code, body = fake_kube.answer(
        state, "/api/v1/namespaces/app-c/secrets", "Bearer t0ken"
    )

    assert code == 403
    assert "cannot list resource" in body["message"]


def test_a_selector_that_does_not_parse_is_a_400(state):
    code, body = fake_kube.answer(
        state, f"{LIST}?labelSelector=team+in+%28", "Bearer t0ken"
    )

    assert code == 400
    assert "unable to parse requirement" in body["message"]


def test_the_exporter_lists_it_like_the_real_api(state):
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

    found = client.list_secrets("app-b", "s3-exporter/watch=true")
    assert sorted(found) == ["one-s3", "two-s3"]
    assert found["two-s3"].data["S3_BUCKET"] == "two"
    with pytest.raises(SecretForbidden):
        client.list_secrets("app-c", "s3-exporter/watch=true")


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
    from exporter import discovery_interval, parse_configuration
    from exporter.buckets import parse_plan

    state = load("state")
    settings = parse_configuration(state.config_text(add_spare=True).encode())

    class Kube:
        def default_namespace(self):
            return "s3-exporter"

    environ = {"AWS_DEFAULT_REGION": "us-east-1", "OPENSHIFT_NAMESPACE": "demo"}
    plan = parse_plan(settings, environ, Kube())

    named = [s for s in state.SECRETS if s[0] != state.DISCOVERED]
    assert len(plan.specs) == len(named) + 1 + 1 + len(state.ENV_PREFIXES)
    assert [rule.key for rule in plan.rules] == [
        "selector:app-b/s3-exporter/watch=true"
    ]
    assert discovery_interval(settings) == 15


def test_the_discovered_namespace_s_secrets_are_ones_the_exporter_accepts(
    tmp_path, monkeypatch
):
    """Labelled, annotated and laid out as the exporter expects them."""
    import threading

    from exporter import parse_configuration
    from exporter.buckets import parse_plan
    from exporter.discovery import Discoverer

    stack = load("state")
    (tmp_path / "ca.pem").write_text("ca")
    monkeypatch.setattr(stack, "STATE", tmp_path / "state")
    monkeypatch.setattr(stack, "CA", tmp_path / "ca.pem")
    stack.init()

    class Kube:
        def default_namespace(self):
            return "s3-exporter"

        def list_secrets(self, namespace, selector):
            from urllib.parse import urlencode

            from exporter.kube import ListedSecret

            path = f"/api/v1/namespaces/{namespace}/secrets?" + urlencode(
                {"labelSelector": selector}
            )
            token = (stack.STATE / "sa" / "token").read_text()
            code, body = fake_kube.answer(stack.STATE, path, f"Bearer {token}")
            assert code == 200
            return {
                item["metadata"]["name"]: ListedSecret(
                    {k: base64.b64decode(v).decode() for k, v in item["data"].items()},
                    item["metadata"]["annotations"],
                )
                for item in body["items"]
            }

    environ = {"AWS_DEFAULT_REGION": "us-east-1", "OPENSHIFT_NAMESPACE": "demo"}
    settings = parse_configuration(stack.config_text().encode())
    plan = parse_plan(settings, environ, Kube())
    discovered = [
        spec
        for spec in Discoverer(Kube(), plan, 0, threading.Event()).discover()
        if spec.key.startswith("secret:app-b/")
        and spec.key != "secret:app-b/app-b-pending-s3"
    ]

    expected = [s for s in stack.SECRETS if s[0] == stack.DISCOVERED]
    assert len(discovered) == len(expected)
    buckets = {spec.key: spec.credentials.resolve().bucket for spec in discovered}
    assert buckets["secret:app-b/app-b-media-s3"] == "app-b-media"
    probe_only = [spec.key for spec in discovered if not spec.collect]
    assert probe_only == ["secret:app-b/app-b-exports-s3"]

    stack.label("app-b/app-b-spare-s3")
    stack.annotate("app-b/app-b-spare-s3", "interval", "3")
    spare = [
        spec
        for spec in Discoverer(Kube(), plan, 0, threading.Event()).discover()
        if spec.key == "secret:app-b/app-b-spare-s3"
    ]
    assert spare[0].interval_minutes == 3
