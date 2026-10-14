"""Reading a bucket's credentials from a Secret or the environment, leak-free."""

import base64
import logging

import pytest
import requests

from exporter.credentials import (
    DEFAULT_KEYS,
    CredentialsUnavailable,
    EnvCredentials,
    KubernetesSecretCredentials,
    ResolvedCredentials,
)
from exporter.kube import (
    InClusterKubernetes,
    KubernetesError,
    KubernetesUnavailable,
    SecretForbidden,
    SecretNotFound,
)

ACCESS = "AKIA-SENSITIVE-ACCESS"
SECRET = "sensitive/secret+key=="
KEYS = {"access_key": "S3_KEY", "secret_key": "S3_SECRET", "bucket": "S3_BUCKET"}


def encoded(values):
    return {k: base64.b64encode(v.encode()).decode() for k, v in values.items()}


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    """Answers each GET with the next queued response, recording the call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, verify=None, timeout=None):
        self.calls.append(
            {"url": url, "headers": headers, "verify": verify, "timeout": timeout}
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def sa_dir(tmp_path):
    (tmp_path / "token").write_text("token-1\n")
    (tmp_path / "ca.crt").write_text("CA")
    (tmp_path / "namespace").write_text("monitoring\n")
    return tmp_path


def client(sa_dir, *responses, host="10.0.0.1"):
    session = FakeSession(*responses)
    return InClusterKubernetes(host, "443", sa_dir, session=session), session


def secret_body(values):
    return {"kind": "Secret", "data": encoded(values)}


# --- the Kubernetes client ---------------------------------------------------


def test_a_secret_is_read_and_decoded(sa_dir):
    kube, session = client(sa_dir, FakeResponse(200, secret_body({"S3_KEY": ACCESS})))

    assert kube.read_secret("app-a", "app-a-s3") == {"S3_KEY": ACCESS}
    call = session.calls[0]
    assert (
        call["url"] == "https://10.0.0.1:443/api/v1/namespaces/app-a/secrets/app-a-s3"
    )
    assert call["headers"] == {"Authorization": "Bearer token-1"}
    assert call["verify"] == str(sa_dir / "ca.crt")
    assert call["timeout"]


def test_the_token_is_read_again_on_every_call(sa_dir):
    # Projected tokens expire and the kubelet rewrites the file.
    body = secret_body({"S3_KEY": ACCESS})
    kube, session = client(sa_dir, FakeResponse(200, body), FakeResponse(200, body))
    kube.read_secret("app-a", "s")
    (sa_dir / "token").write_text("token-2")
    kube.read_secret("app-a", "s")

    assert session.calls[1]["headers"] == {"Authorization": "Bearer token-2"}


def test_an_ipv6_service_address_is_bracketed(sa_dir):
    kube, _ = client(sa_dir, host="fd00::1")
    assert kube.base_url == "https://[fd00::1]:443"


def test_a_forbidden_secret_points_at_the_rbac(sa_dir):
    kube, _ = client(sa_dir, FakeResponse(403, {"message": "forbidden"}))

    with pytest.raises(SecretForbidden, match="Role/RoleBinding"):
        kube.read_secret("app-a", "s")


def test_a_missing_secret_says_so(sa_dir):
    kube, _ = client(sa_dir, FakeResponse(404, {}))

    with pytest.raises(SecretNotFound, match="app-a/s does not exist"):
        kube.read_secret("app-a", "s")


def test_an_unexpected_status_keeps_only_the_api_message(sa_dir):
    body = {"message": "Unauthorized", "details": {"leak": SECRET}}
    kube, _ = client(sa_dir, FakeResponse(401, body))

    with pytest.raises(KubernetesUnavailable) as raised:
        kube.read_secret("app-a", "s")
    assert "HTTP 401: Unauthorized" in str(raised.value)
    assert SECRET not in str(raised.value)


def test_an_unreachable_api_is_reported(sa_dir):
    kube, _ = client(sa_dir, requests.ConnectionError("refused"))

    with pytest.raises(KubernetesUnavailable, match="cannot reach"):
        kube.read_secret("app-a", "s")


def test_a_tls_failure_points_at_the_ca(sa_dir):
    kube, _ = client(sa_dir, requests.exceptions.SSLError("certificate verify failed"))

    with pytest.raises(KubernetesUnavailable, match=r"TLS verification.*ca\.crt"):
        kube.read_secret("app-a", "s")


def test_a_value_that_is_not_base64_names_the_key_only(sa_dir):
    body = {"data": {"S3_KEY": "%%%not-base64%%%"}}
    kube, _ = client(sa_dir, FakeResponse(200, body))

    with pytest.raises(KubernetesUnavailable) as raised:
        kube.read_secret("app-a", "s")
    assert "S3_KEY" in str(raised.value)
    assert "%%%" not in str(raised.value)


@pytest.mark.parametrize(
    "namespace, name",
    [("app-a", "../../nodes"), ("app-a/x", "s"), ("App", "s"), ("app-a", "")],
)
def test_a_name_that_would_change_the_api_path_is_refused(sa_dir, namespace, name):
    kube, session = client(sa_dir)

    with pytest.raises(KubernetesError, match="invalid secret reference"):
        kube.read_secret(namespace, name)
    assert session.calls == []


def test_outside_a_pod_there_is_no_client(tmp_path):
    assert InClusterKubernetes.detect({}, tmp_path) is None
    env = {"KUBERNETES_SERVICE_HOST": "10.0.0.1", "KUBERNETES_SERVICE_PORT": "443"}
    # Variables alone are not enough: no token, no API access.
    assert InClusterKubernetes.detect(env, tmp_path) is None


def test_inside_a_pod_the_client_is_found(sa_dir):
    env = {"KUBERNETES_SERVICE_HOST": "10.0.0.1", "KUBERNETES_SERVICE_PORT": "6443"}
    kube = InClusterKubernetes.detect(env, sa_dir)

    assert kube.base_url == "https://10.0.0.1:6443"
    assert kube.default_namespace() == "monitoring"


# --- the providers -------------------------------------------------------------


def test_a_secret_provides_credentials_and_the_bucket_name(sa_dir):
    values = {"S3_KEY": ACCESS, "S3_SECRET": SECRET, "S3_BUCKET": "uploads"}
    kube, _ = client(sa_dir, FakeResponse(200, secret_body(values)))
    source = KubernetesSecretCredentials(kube, "app-a", "app-a-s3", KEYS)

    resolved = source.resolve()

    assert (resolved.access_key, resolved.secret_key, resolved.bucket) == (
        ACCESS,
        SECRET,
        "uploads",
    )
    assert source.describe() == "secret app-a/app-a-s3"


def test_a_configured_name_wins_over_the_secret(sa_dir):
    values = {"S3_KEY": ACCESS, "S3_SECRET": SECRET}
    kube, _ = client(sa_dir, FakeResponse(200, secret_body(values)))
    source = KubernetesSecretCredentials(kube, "app-a", "s", KEYS, bucket_name="mine")

    assert source.resolve().bucket == "mine"


def test_a_missing_key_is_named(sa_dir):
    kube, _ = client(sa_dir, FakeResponse(200, secret_body({"S3_KEY": ACCESS})))
    source = KubernetesSecretCredentials(kube, "app-a", "s", KEYS)

    with pytest.raises(CredentialsUnavailable, match="secret app-a/s: S3_SECRET is"):
        source.resolve()


def test_an_api_error_becomes_unavailable_credentials(sa_dir):
    kube, _ = client(sa_dir, FakeResponse(403, {}))
    source = KubernetesSecretCredentials(kube, "app-a", "s", KEYS)

    with pytest.raises(CredentialsUnavailable, match="forbidden") as raised:
        source.resolve()
    # The chain is dropped: nothing underneath can surface in a traceback.
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


def test_envfrom_variables_are_prefix_plus_key():
    environ = {
        "APP_C_S3_KEY": ACCESS,
        "APP_C_S3_SECRET": SECRET,
        "APP_C_S3_BUCKET": "b",
    }
    source = EnvCredentials("APP_C_", KEYS, environ)

    resolved = source.resolve()

    assert (resolved.access_key, resolved.bucket) == (ACCESS, "b")
    assert source.describe() == "environment APP_C_*"


@pytest.mark.parametrize("value", [None, ""])
def test_a_missing_or_empty_variable_is_named(value):
    environ = {"APP_C_S3_KEY": ACCESS, "APP_C_S3_BUCKET": "b"}
    if value is not None:
        environ["APP_C_S3_SECRET"] = value
    source = EnvCredentials("APP_C_", KEYS, environ)

    with pytest.raises(CredentialsUnavailable, match="APP_C_S3_SECRET is missing"):
        source.resolve()


def test_the_single_bucket_names_are_the_aws_ones():
    environ = {
        "AWS_ACCESS_KEY_ID": ACCESS,
        "AWS_SECRET_ACCESS_KEY": SECRET,
        "S3_BUCKET_NAME": "b",
    }
    assert EnvCredentials("", DEFAULT_KEYS, environ).resolve().bucket == "b"


# --- nothing leaks ---------------------------------------------------------------


def test_resolved_credentials_hide_their_values():
    resolved = ResolvedCredentials(access_key=ACCESS, secret_key=SECRET, bucket="b")

    assert ACCESS not in repr(resolved)
    assert SECRET not in repr(resolved)
    assert "b" in repr(resolved)


def test_no_value_reaches_the_logs_or_the_errors(sa_dir, caplog):
    # A Secret with the access key but not the secret key: the error path runs
    # with sensitive values already in hand.
    kube, _ = client(sa_dir, FakeResponse(200, secret_body({"S3_KEY": ACCESS})))
    source = KubernetesSecretCredentials(kube, "app-a", "s", KEYS)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(CredentialsUnavailable) as raised:
            source.resolve()
        logging.error("resolve failed: %s", raised.value)

    assert ACCESS not in str(raised.value)
    assert ACCESS not in caplog.text
