"""The fake cluster's state, and the faults `make obs/kube/*` puts into it.

Everything the multi-bucket exporter reads from "Kubernetes" lives under
kube/state (never committed), rebuilt by `init`:

- sa/            its ServiceAccount: token, ca.crt, namespace
- secrets.json   the applications' Secrets, by `namespace/name`
- rbac.json      the Secrets each Role lets the ServiceAccount `get`
- config/        its collector.yml, a ConfigMap volume: every change is a new
                 directory and an atomic swap of the `..data` link, as the
                 kubelet does it

Usage: state.py init|reset|forbid|allow|delete|wrong-key|repoint|config-add|
       config-remove|config-broken [args]
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
import time
from pathlib import Path

STATE = Path(os.environ.get("KUBE_STATE", "/kube/state"))
CA = Path(os.environ.get("KUBE_CA", "/tls/ca.pem"))
ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "s3admin")
SECRET_KEY = os.environ.get("S3_SECRET_KEY", "s3admin-secret")

TLS_ENDPOINT = "https://s3.storage.internal:8443"

# (namespace, Secret name, bucket, extra keys of the entry). The applications
# name their Secrets as they please; the key names are the same in all of them,
# and not the AWS ones -- as in production.
SECRETS = [
    ("app-a", "app-a-uploads-s3", "app-a-uploads", {}),
    ("app-a", "app-a-archive-s3", "app-a-archive", {}),
    ("app-a", "app-a-reports-s3", "app-a-reports", {}),
    ("app-a", "app-a-media-s3", "app-a-media", {}),
    ("app-a", "app-a-exports-s3", "app-a-exports", {}),
    ("app-a", "app-a-tls-s3", "bucket-tls", {"endpoint_url": TLS_ENDPOINT}),
    ("app-b", "app-b-uploads-s3", "app-b-uploads", {}),
    ("app-b", "app-b-archive-s3", "app-b-archive", {}),
    ("app-b", "app-b-reports-s3", "app-b-reports", {}),
    ("app-b", "app-b-media-s3", "app-b-media", {}),
    # Too large to list, say: probed only.
    ("app-b", "app-b-exports-s3", "app-b-exports", {"collect": "false"}),
    ("app-c", "app-c-uploads-s3", "app-c-uploads", {}),
    ("app-c", "app-c-archive-s3", "app-c-archive", {}),
    ("app-c", "app-c-reports-s3", "app-c-reports", {}),
    # The heavy one: 50 000 objects, collected less often.
    ("app-c", "app-c-large-s3", "bucket-large", {"interval": "10"}),
]
# Listed in the configuration, but nobody created the Secret yet: shows as not
# watched until `make obs/kube/reset` or a Secret appears.
PENDING = ("app-b", "app-b-pending-s3")
# Created, granted, but not in the configuration until `config-add`.
SPARE = ("app-a", "app-a-spare-s3", "bucket-small")
# The fallback: variables laid out by envFrom with a prefix (see compose.yaml).
ENV_PREFIXES = [("app-c", "APP_C_MEDIA_"), ("app-c", "APP_C_EXPORTS_")]


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o644)
    os.replace(tmp, path)


def _load(name: str) -> dict:
    return json.loads((STATE / name).read_text(encoding="utf-8"))


def _save(name: str, value: dict):
    _write(STATE / name, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _secret(bucket: str) -> dict:
    return {"S3_KEY": ACCESS_KEY, "S3_SECRET": SECRET_KEY, "S3_BUCKET": bucket}


def _entry(namespace: str, name: str, extra: dict) -> str:
    fields = [f"secret: {name}", f"namespace: {namespace}"]
    fields += [f"{key}: {value}" for key, value in extra.items()]
    return "      - { " + ", ".join(fields) + " }"


def config_text(add_spare=False, remove=()) -> str:
    entries = [
        _entry(namespace, name, extra)
        for namespace, name, _, extra in SECRETS
        if name not in remove
    ]
    entries.append(_entry(*PENDING, {}))
    if add_spare:
        entries.append(_entry(SPARE[0], SPARE[1], {}))
    entries += [
        f"      - {{ env_prefix: {prefix}, namespace: {namespace} }}"
        for namespace, prefix in ENV_PREFIXES
    ]
    return (
        "# Written by test/observability/kube/state.py: edit it there, or use\n"
        "# make obs/kube/config-*; this copy is replaced on reset.\n"
        "kind: PrometheusExporterConfig\n"
        "configuration:\n"
        "  # Two minutes, to see collections move in a demo; production keeps 60.\n"
        "  - interval: 2\n"
        "  - connectivity_interval: 30\n"
        "  - config_reload_interval: 15\n"
        "  # A minute, so a fixed Secret or RoleBinding shows quickly; production keeps 300.\n"
        "  - credentials_refresh: 60\n"
        "  - secret_keys: { access_key: S3_KEY, secret_key: S3_SECRET, bucket: S3_BUCKET }\n"
        "  - buckets:\n" + "\n".join(entries) + "\n"
    )


def publish_config(text: str):
    """Swap a new collector.yml in, the way the kubelet updates a ConfigMap."""
    config = STATE / "config"
    config.mkdir(parents=True, exist_ok=True)
    version = config / f"..{time.strftime('%Y_%m_%d_%H_%M_%S')}.{time.time_ns()}"
    version.mkdir()
    _write(version / "collector.yml", text)
    version.chmod(0o755)
    link = config / "..data_tmp"
    if link.is_symlink():
        link.unlink()
    link.symlink_to(version.name)
    os.replace(link, config / "..data")
    if not (config / "collector.yml").is_symlink():
        (config / "collector.yml").symlink_to("..data/collector.yml")
    # Old versions go, as the kubelet removes them.
    for old in config.glob("..20*"):
        if old.name != version.name:
            shutil.rmtree(old)


def init(force=False):
    if (STATE / "secrets.json").exists() and not force:
        print(f"state already in {STATE}")
        return
    sa = STATE / "sa"
    sa.mkdir(parents=True, exist_ok=True)
    sa.chmod(0o755)
    STATE.chmod(0o755)
    _write(sa / "token", secrets.token_urlsafe(32))
    _write(sa / "namespace", "s3-exporter")
    _write(sa / "ca.crt", CA.read_text(encoding="utf-8"))

    all_secrets = {f"{ns}/{name}": _secret(bucket) for ns, name, bucket, _ in SECRETS}
    all_secrets[f"{SPARE[0]}/{SPARE[1]}"] = _secret(SPARE[2])
    _save("secrets.json", all_secrets)
    # One Role per application namespace, restricted to the Secrets it lists;
    # the pending one is granted, it just does not exist.
    granted = sorted(set(all_secrets) | {f"{PENDING[0]}/{PENDING[1]}"})
    _save("rbac.json", {"get_secrets": granted})
    publish_config(config_text())
    print(f"state written to {STATE}: {len(all_secrets)} secrets")


def _ref(name: str) -> str:
    if "/" not in name:
        sys.exit(f"name the Secret as namespace/name, got {name!r}")
    return name


def forbid(name):
    rbac = _load("rbac.json")
    rbac["get_secrets"] = [ref for ref in rbac["get_secrets"] if ref != _ref(name)]
    _save("rbac.json", rbac)
    print(f"{name}: Role no longer grants `get` (403)")


def allow(name):
    rbac = _load("rbac.json")
    rbac["get_secrets"] = sorted(set(rbac["get_secrets"]) | {_ref(name)})
    _save("rbac.json", rbac)
    print(f"{name}: granted")


def delete(name):
    found = _load("secrets.json")
    found.pop(_ref(name), None)
    _save("secrets.json", found)
    print(f"{name}: deleted (404)")


def wrong_key(name):
    found = _load("secrets.json")
    found[_ref(name)]["S3_KEY"] = "not-a-key"
    _save("secrets.json", found)
    print(f"{name}: S3_KEY no longer valid (S3 answers InvalidAccessKeyId)")


def repoint(name, bucket):
    found = _load("secrets.json")
    found[_ref(name)]["S3_BUCKET"] = bucket
    _save("secrets.json", found)
    print(f"{name}: now names bucket {bucket}")


COMMANDS = {
    "init": lambda: init(),
    "reset": lambda: init(force=True),
    "forbid": forbid,
    "allow": allow,
    "delete": delete,
    "wrong-key": wrong_key,
    "repoint": repoint,
    "config-add": lambda: publish_config(config_text(add_spare=True)),
    "config-remove": lambda: publish_config(config_text(remove={"app-a-reports-s3"})),
    "config-broken": lambda: publish_config("configuration:\n  - buckets: [ {\n"),
}


def main(argv):
    if not argv or argv[0] not in COMMANDS:
        sys.exit(__doc__)
    COMMANDS[argv[0]](*argv[1:])


if __name__ == "__main__":
    main(sys.argv[1:])
