"""A reload applied as a difference: only the buckets that changed move."""

import logging

import pytest

from exporter.buckets import BucketSpec, parse_buckets
from exporter.credentials import EnvCredentials
from exporter.fleet import Fleet

ENV = {"AWS_DEFAULT_REGION": "us-east-1", "OPENSHIFT_NAMESPACE": "demo"}


def specs(*entries, secret_keys=None):
    settings = {"buckets": [dict(entry) for entry in entries]}
    if secret_keys:
        settings["secret_keys"] = secret_keys
    return parse_buckets(settings, ENV, None)


class Watched:
    def __init__(self, spec):
        self.spec = spec
        self.endpoint = spec.endpoint_url or None
        self.refreshed = 0
        self.stopped = False

    def refresh_credentials(self):
        self.refreshed += 1

    def stop(self):
        self.stopped = True


class Endpoints:
    def __init__(self):
        self.synced = []

    def sync(self, endpoints):
        self.synced.append(sorted(endpoints))


def fleet(initial, endpoints=None):
    changes = []
    watched = Fleet(
        [Watched(spec) for spec in initial],
        Watched,
        endpoints,
        on_change=lambda: changes.append(1),
    )
    return watched, changes


A = {"env_prefix": "A_"}
B = {"env_prefix": "B_"}
C = {"env_prefix": "C_"}


def test_it_iterates_like_a_list_of_its_monitors():
    watched, _ = fleet(specs(A, B))
    assert [m.spec.key for m in watched] == ["env:A_", "env:B_"]
    assert len(watched) == 2


def test_an_unchanged_bucket_keeps_its_monitor():
    watched, changes = fleet(specs(A, B))
    before = watched.get("env:A_")

    change = watched.apply(specs(A, B))

    assert not change
    assert change.unchanged == 2
    assert watched.get("env:A_") is before
    assert not before.stopped
    assert changes == []


def test_added_removed_and_changed_buckets(caplog):
    watched, changes = fleet(specs(A, B))
    a, b = watched.get("env:A_"), watched.get("env:B_")

    with caplog.at_level(logging.INFO):
        change = watched.apply(specs(dict(A, interval=120), C))

    assert change.added == ["env:C_"]
    assert change.removed == ["env:B_"]
    assert change.changed == ["env:A_"]
    assert change.summary() == "1 added, 1 removed, 1 changed, 0 unchanged"
    # The changed one is watched afresh, the removed one stopped.
    assert a.stopped and b.stopped
    assert watched.get("env:A_") is not a
    assert watched.get("env:A_").refreshed == 1
    assert watched.get("env:C_").refreshed == 1
    assert [m.spec.key for m in watched] == ["env:A_", "env:C_"]
    assert changes == [1]
    assert "Bucket env:B_ removed" in caplog.text


def test_the_inherited_key_names_count_as_a_change():
    watched, _ = fleet(specs(A))

    change = watched.apply(specs(A, secret_keys={"access_key": "S3_KEY"}))

    assert change.changed == ["env:A_"]


def test_the_endpoints_follow_the_buckets():
    endpoints = Endpoints()
    watched, _ = fleet(specs(dict(A, endpoint_url="https://grid:8082")), endpoints)

    watched.apply(
        specs(
            dict(A, endpoint_url="https://grid:8082"),
            dict(B, endpoint_url="https://other:443"),
        )
    )
    watched.apply(specs(dict(B, endpoint_url="https://other:443")))

    assert endpoints.synced == [
        ["https://grid:8082", "https://other:443"],
        ["https://other:443"],
    ]


def test_the_order_follows_the_file():
    watched, _ = fleet(specs(A, B))
    watched.apply(specs(C, B, A))
    assert [m.spec.key for m in watched] == ["env:C_", "env:B_", "env:A_"]


# --- what counts as the same entry ----------------------------------------------


def spec(**overrides):
    values = dict(
        key="env:A_",
        credentials=EnvCredentials(
            "A_", {"access_key": "K", "secret_key": "S", "bucket": "B"}, {}
        ),
        label_namespace="demo",
        endpoint_url="https://grid:8082",
        region="us-east-1",
    )
    values.update(overrides)
    return BucketSpec(**values)


def test_two_identical_entries_have_the_same_fingerprint():
    assert spec().fingerprint() == spec().fingerprint()


@pytest.mark.parametrize(
    "overrides",
    [
        {"label_namespace": "other"},
        {"endpoint_url": "https://other:8082"},
        {"region": "eu-west-1"},
        {"interval_minutes": 120},
        {"source": "list"},
        {"collect": False},
        {
            "credentials": EnvCredentials(
                "A_", {"access_key": "X", "secret_key": "S", "bucket": "B"}, {}
            )
        },
        {
            "credentials": EnvCredentials(
                "A_",
                {"access_key": "K", "secret_key": "S", "bucket": "B"},
                {},
                bucket_name="named",
            )
        },
    ],
)
def test_every_setting_of_an_entry_counts(overrides):
    assert spec(**overrides).fingerprint() != spec().fingerprint()


def test_a_fingerprint_never_holds_a_value():
    credentials = EnvCredentials(
        "A_",
        {"access_key": "K", "secret_key": "S", "bucket": "B"},
        {"A_K": "AKIA-secret-value", "A_S": "very-secret", "A_B": "bucket"},
    )
    fingerprint = repr(spec(credentials=credentials).fingerprint())
    assert "AKIA-secret-value" not in fingerprint
    assert "very-secret" not in fingerprint
