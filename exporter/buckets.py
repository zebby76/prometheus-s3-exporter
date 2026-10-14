"""Which buckets to watch, and where each one's credentials come from.

Without `buckets:` in the configuration the exporter watches one bucket, named
by the environment as it always has. With it, each entry names a Kubernetes
Secret -- or, as a fallback, an environment prefix -- and the Secret holds the
bucket name and its credentials.

Everything a human has to fix is checked here, at startup, and stops the
exporter: a typo in one of a hundred entries must show as a CrashLoopBackOff,
not as a bucket silently missing from the dashboards. What can fix itself -- a
Secret not readable yet, a RoleBinding still to come -- is not checked here; it
surfaces when the credentials are resolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Mapping, Optional

from exporter.credentials import (
    DEFAULT_KEYS,
    CredentialSource,
    EnvCredentials,
    KubernetesSecretCredentials,
)
from exporter.kube import InClusterKubernetes, valid_namespace, valid_secret_name
from exporter.sources import AUTO, SOURCES

ENTRY_KEYS = (
    "secret",
    "namespace",
    "env_prefix",
    "name",
    "secret_keys",
    "endpoint_url",
    "region",
    "interval",
    "collect",
    "source",
)


class ConfigError(Exception):
    """The configuration cannot be honoured; the exporter must not start."""


@dataclass(frozen=True)
class BucketSpec:
    """One bucket to watch, as configured -- before anything is read."""

    # Stable identity across reloads: `secret:<ns>/<name>` or `env:<prefix>`.
    key: str
    credentials: CredentialSource
    # The namespace of the application that owns the bucket, for the label.
    label_namespace: str
    endpoint_url: str
    region: str
    # Per-bucket overrides; None means the global setting.
    interval_minutes: Optional[float] = None
    source: Optional[str] = None
    # False: probe the bucket, never list it -- for one too large to list.
    collect: bool = True

    def fingerprint(self) -> tuple:
        """Every setting of the entry: two specs with the same one watch the
        same bucket the same way, and a reload leaves that bucket alone."""
        return (
            self.key,
            self.credentials.identity(),
            self.label_namespace,
            self.endpoint_url,
            self.region,
            self.interval_minutes,
            self.source,
            self.collect,
        )


def multi_bucket(settings: Mapping) -> bool:
    """True when the configuration lists buckets.

    `buckets:` with nothing but comments under it parses as None and counts
    as absent, like an empty `sources:`.
    """
    return settings.get("buckets") is not None


def _secret_keys(value, where: str, base: Mapping[str, str]) -> dict:
    if value is None:
        return dict(base)
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: secret_keys must be a mapping")
    unknown = sorted(set(value) - set(DEFAULT_KEYS))
    if unknown:
        raise ConfigError(
            f"{where}: unknown secret_keys field(s) {', '.join(unknown)} "
            f"(known: {', '.join(DEFAULT_KEYS)})"
        )
    merged = dict(base)
    for field_name, key in value.items():
        if not isinstance(key, str) or not key:
            raise ConfigError(f"{where}: secret_keys.{field_name} must be a name")
        merged[field_name] = key
    return merged


def _optional_text(entry: Mapping, key: str, where: str) -> Optional[str]:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}: {key} must be a non-empty string")
    return value


def _interval(entry: Mapping, where: str) -> Optional[float]:
    value = entry.get("interval")
    if value is None:
        return None
    # bool is an int: `interval: true` must not read as one minute.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{where}: interval must be a number of minutes above 0")
    return float(value)


def _collect(entry: Mapping, where: str) -> bool:
    value = entry.get("collect", True)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: collect must be true or false")
    return value


def _source(entry: Mapping, where: str) -> Optional[str]:
    value = entry.get("source")
    if value is None:
        return None
    known = [AUTO] + [candidate.name for candidate in SOURCES]
    if not isinstance(value, str) or value.strip().lower() not in known:
        raise ConfigError(
            f"{where}: source must be one of {', '.join(known)}, got {value!r}"
        )
    return value.strip().lower()


def parse_buckets(
    settings: Mapping,
    environ: Mapping[str, str],
    kube: Optional[InClusterKubernetes],
) -> List[BucketSpec]:
    """The configured buckets, or ConfigError for anything a human must fix."""
    keys = _secret_keys(settings.get("secret_keys"), "secret_keys", DEFAULT_KEYS)

    entries = settings.get("buckets")
    if not isinstance(entries, list):
        raise ConfigError("buckets must be a list")
    if not entries:
        raise ConfigError("buckets is empty: list at least one, or remove the key")

    default_endpoint = environ.get("AWS_ENDPOINT_URL", "")
    default_region = environ.get("AWS_DEFAULT_REGION", "")

    specs = []
    seen = set()
    for index, entry in enumerate(entries, start=1):
        where = f"buckets[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} must be a mapping")

        unknown = sorted(set(entry) - set(ENTRY_KEYS))
        if unknown:
            raise ConfigError(
                f"{where}: unknown key(s) {', '.join(unknown)} "
                f"(known: {', '.join(ENTRY_KEYS)})"
            )

        secret = entry.get("secret")
        prefix = entry.get("env_prefix")
        if (secret is None) == (prefix is None):
            raise ConfigError(f"{where}: set exactly one of `secret` or `env_prefix`")

        entry_keys = _secret_keys(entry.get("secret_keys"), where, keys)
        name = _optional_text(entry, "name", where)
        namespace = _optional_text(entry, "namespace", where)
        endpoint_url = _optional_text(entry, "endpoint_url", where) or default_endpoint
        region = _optional_text(entry, "region", where) or default_region
        if not region:
            raise ConfigError(
                f"{where}: no region (set `region` or AWS_DEFAULT_REGION)"
            )

        source: CredentialSource
        if secret is not None:
            if kube is None:
                raise ConfigError(
                    f"{where}: `secret` needs the Kubernetes API, and this process "
                    "does not run in a pod (no ServiceAccount token); use "
                    "`env_prefix` outside a cluster"
                )
            if not valid_secret_name(secret):
                raise ConfigError(f"{where}: {secret!r} is not a valid Secret name")
            namespace = namespace or kube.default_namespace()
            if not namespace:
                raise ConfigError(
                    f"{where}: no namespace, and the pod's own is unknown"
                )
            if not valid_namespace(namespace):
                raise ConfigError(f"{where}: {namespace!r} is not a valid namespace")
            key = f"secret:{namespace}/{secret}"
            source = KubernetesSecretCredentials(
                kube, namespace, secret, entry_keys, bucket_name=name
            )
        else:
            if not isinstance(prefix, str) or not prefix:
                raise ConfigError(f"{where}: env_prefix must be a non-empty string")
            namespace = namespace or environ.get("OPENSHIFT_NAMESPACE", "")
            if not namespace:
                raise ConfigError(
                    f"{where}: no namespace for the label "
                    "(set `namespace` or OPENSHIFT_NAMESPACE)"
                )
            key = f"env:{prefix}"
            source = EnvCredentials(prefix, entry_keys, environ, bucket_name=name)

        if key in seen:
            raise ConfigError(f"{where}: {key} is listed twice")
        seen.add(key)

        specs.append(
            BucketSpec(
                key=key,
                credentials=source,
                label_namespace=namespace,
                endpoint_url=endpoint_url,
                region=region,
                interval_minutes=_interval(entry, where),
                source=_source(entry, where),
                collect=_collect(entry, where),
            )
        )
    return specs


def single_bucket_spec(environ: Mapping[str, str]) -> BucketSpec:
    """The one bucket of a configuration without `buckets:`, as before 6.0.0."""
    return BucketSpec(
        key="env:",
        credentials=EnvCredentials("", DEFAULT_KEYS, environ),
        label_namespace=environ.get("OPENSHIFT_NAMESPACE", ""),
        endpoint_url=environ.get("AWS_ENDPOINT_URL", ""),
        region=environ.get("AWS_DEFAULT_REGION", ""),
    )
