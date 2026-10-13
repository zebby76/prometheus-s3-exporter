"""Where a bucket's S3 credentials and name come from.

Two sources, behind one interface:

- a Kubernetes Secret, read through the API with the pod's ServiceAccount: the
  Secret stays in the namespace of the application that owns the bucket, and a
  rotation is picked up without restarting the exporter;
- environment variables, as `envFrom` with a `prefix` lays them out.

Both use the same key names. `envFrom` with prefix `APP_A_` turns the Secret's
key `ACCESS_KEY` into the variable `APP_A_ACCESS_KEY`, so one `secret_keys`
setting describes a Secret whichever way it reaches the exporter.

No value read here ever reaches a log line, an exception or a repr: messages
name the variable, the key or the Secret, never what they hold.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Mapping, Optional

from exporter.kube import InClusterKubernetes, KubernetesError

# The AWS names: what a deployment that sets nothing gets, and what the
# single-bucket mode has always read.
DEFAULT_KEYS = {
    "access_key": "AWS_ACCESS_KEY_ID",
    "secret_key": "AWS_SECRET_ACCESS_KEY",
    "bucket": "S3_BUCKET_NAME",
}


class CredentialsUnavailable(Exception):
    """A bucket's credentials could not be read. Never holds a value."""


@dataclass(frozen=True)
class ResolvedCredentials:
    # Out of repr, so a stray log of the object leaks nothing.
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    bucket: str


class CredentialSource(ABC):
    """Reads one bucket's credentials and name, afresh on every call."""

    def __init__(self, keys: Mapping[str, str], bucket_name: Optional[str] = None):
        self.keys = dict(keys)
        # Set when the configuration names the bucket itself, for a Secret that
        # does not carry its name.
        self.bucket_name = bucket_name

    @abstractmethod
    def describe(self) -> str:
        """Where the credentials come from, for logs and reports."""

    @abstractmethod
    def _values(self) -> Mapping[str, str]:
        """Every value available, by the name it is looked up with."""

    @abstractmethod
    def _lookup_name(self, key: str) -> str:
        """The name `key` is found under: a Secret key or a variable."""

    def resolve(self) -> ResolvedCredentials:
        values = self._values()
        wanted = ["access_key", "secret_key"]
        if not self.bucket_name:
            wanted.append("bucket")

        found = {}
        for field_name in wanted:
            lookup = self._lookup_name(self.keys[field_name])
            value = values.get(lookup, "")
            if not value:
                raise CredentialsUnavailable(
                    f"{self.describe()}: {lookup} is missing or empty"
                )
            found[field_name] = value

        return ResolvedCredentials(
            access_key=found["access_key"],
            secret_key=found["secret_key"],
            bucket=self.bucket_name or found["bucket"],
        )


class EnvCredentials(CredentialSource):
    """Variables named `prefix + key`, as `envFrom` with a prefix sets them."""

    def __init__(
        self,
        prefix: str,
        keys: Mapping[str, str],
        environ: Mapping[str, str],
        bucket_name: Optional[str] = None,
    ):
        super().__init__(keys, bucket_name)
        self.prefix = prefix
        self._environ = environ

    def describe(self) -> str:
        return f"environment {self.prefix}*" if self.prefix else "environment"

    def _values(self) -> Mapping[str, str]:
        return self._environ

    def _lookup_name(self, key: str) -> str:
        return f"{self.prefix}{key}"


class KubernetesSecretCredentials(CredentialSource):
    """A Secret read through the Kubernetes API on every resolve."""

    def __init__(
        self,
        client: InClusterKubernetes,
        namespace: str,
        secret: str,
        keys: Mapping[str, str],
        bucket_name: Optional[str] = None,
    ):
        super().__init__(keys, bucket_name)
        self.client = client
        self.namespace = namespace
        self.secret = secret

    def describe(self) -> str:
        return f"secret {self.namespace}/{self.secret}"

    def _values(self) -> Mapping[str, str]:
        try:
            return self.client.read_secret(self.namespace, self.secret)
        except KubernetesError as exc:
            # Its message is already value-free; the chain is dropped all the
            # same, so nothing below it can surface in a traceback.
            raise CredentialsUnavailable(str(exc)) from None

    def _lookup_name(self, key: str) -> str:
        return key
