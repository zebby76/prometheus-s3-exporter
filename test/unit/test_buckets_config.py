"""The `buckets:` configuration: what it accepts, and what stops the start."""

import io

import pytest

from exporter import bucket_specs, check_config, load_configuration
from exporter.buckets import ConfigError, multi_bucket, parse_buckets
from exporter.credentials import (
    CredentialsUnavailable,
    EnvCredentials,
    KubernetesSecretCredentials,
)

ENV = {"AWS_DEFAULT_REGION": "us-east-1", "AWS_ENDPOINT_URL": "https://grid:8082"}


class StubKube:
    """The parts of InClusterKubernetes the configuration looks at."""

    def __init__(self, namespace="monitoring", secrets=None):
        self.namespace = namespace
        self.secrets = secrets or {}

    def default_namespace(self):
        return self.namespace

    def read_secret(self, namespace, name):
        from exporter.kube import SecretForbidden

        try:
            return self.secrets[(namespace, name)]
        except KeyError:
            raise SecretForbidden(f"reading secret {namespace}/{name} is forbidden")


def parse(buckets, kube=StubKube(), environ=ENV, **settings):
    return parse_buckets({"buckets": buckets, **settings}, environ, kube)


# --- what it accepts -----------------------------------------------------------


def test_a_secret_entry():
    (spec,) = parse([{"secret": "app-a-s3", "namespace": "app-a"}])

    assert isinstance(spec.credentials, KubernetesSecretCredentials)
    assert spec.key == "secret:app-a/app-a-s3"
    assert spec.label_namespace == "app-a"
    assert spec.endpoint_url == "https://grid:8082"
    assert spec.region == "us-east-1"


def test_the_namespace_defaults_to_the_pods_own():
    (spec,) = parse([{"secret": "s"}])

    assert spec.label_namespace == "monitoring"
    assert spec.key == "secret:monitoring/s"


def test_global_key_names_apply_and_merge_with_overrides():
    specs = parse(
        [
            {"secret": "a"},
            {"secret": "b", "secret_keys": {"access_key": "OTHER_KEY"}},
        ],
        secret_keys={"access_key": "S3_KEY", "secret_key": "S3_SECRET"},
    )

    assert specs[0].credentials.keys == {
        "access_key": "S3_KEY",
        "secret_key": "S3_SECRET",
        "bucket": "S3_BUCKET_NAME",
    }
    assert specs[1].credentials.keys["access_key"] == "OTHER_KEY"
    assert specs[1].credentials.keys["secret_key"] == "S3_SECRET"


def test_per_bucket_endpoint_region_and_name():
    (spec,) = parse(
        [
            {
                "secret": "s",
                "name": "uploads",
                "endpoint_url": "https://other:8082",
                "region": "eu-west-1",
            }
        ]
    )

    assert spec.endpoint_url == "https://other:8082"
    assert spec.region == "eu-west-1"
    assert spec.credentials.bucket_name == "uploads"


def test_an_env_prefix_entry_works_outside_a_cluster():
    (spec,) = parse(
        [{"env_prefix": "APP_C_", "namespace": "app-c"}],
        kube=None,
    )

    assert isinstance(spec.credentials, EnvCredentials)
    assert spec.key == "env:APP_C_"
    assert spec.label_namespace == "app-c"


def test_an_env_prefix_label_defaults_to_openshift_namespace():
    (spec,) = parse(
        [{"env_prefix": "APP_C_"}],
        environ={**ENV, "OPENSHIFT_NAMESPACE": "tools"},
    )
    assert spec.label_namespace == "tools"


def test_buckets_with_only_comments_count_as_absent():
    assert not multi_bucket({"buckets": None})
    assert multi_bucket({"buckets": []})


# --- what stops the start -------------------------------------------------------


@pytest.mark.parametrize(
    "buckets, message",
    [
        ("app-a", "must be a list"),
        ([], "is empty"),
        (["app-a"], "must be a mapping"),
        ([{}], "exactly one of"),
        ([{"secret": "s", "env_prefix": "P_"}], "exactly one of"),
        ([{"secret": "s", "intervall": 5}], "unknown key(s) intervall"),
        ([{"secret": "s", "secret_keys": "S3_KEY"}], "must be a mapping"),
        ([{"secret": "s", "secret_keys": {"acces_key": "K"}}], "acces_key"),
        ([{"secret": "s", "secret_keys": {"access_key": ""}}], "must be a name"),
        ([{"secret": "s", "name": ""}], "name must be a non-empty string"),
        ([{"secret": "Bad_Name"}], "not a valid Secret name"),
        ([{"secret": "s", "namespace": "a/b"}], "not a valid namespace"),
        ([{"env_prefix": ""}], "env_prefix must be"),
        ([{"secret": "s"}, {"secret": "s"}], "listed twice"),
        ([{"env_prefix": "P_"}, {"env_prefix": "P_"}], "listed twice"),
    ],
)
def test_a_configuration_a_human_must_fix_stops_the_start(buckets, message):
    environ = {**ENV, "OPENSHIFT_NAMESPACE": "tools"}
    with pytest.raises(ConfigError) as raised:
        parse(buckets, environ=environ)
    assert message in str(raised.value)


def test_the_same_secret_in_two_namespaces_is_two_buckets():
    specs = parse(
        [{"secret": "s", "namespace": "app-a"}, {"secret": "s", "namespace": "app-b"}]
    )
    assert len(specs) == 2


def test_secret_mode_outside_a_cluster_stops_the_start():
    with pytest.raises(ConfigError, match="does not run in a pod"):
        parse([{"secret": "s"}], kube=None)


def test_no_region_stops_the_start():
    with pytest.raises(ConfigError, match="no region"):
        parse([{"secret": "s"}], environ={})


def test_no_namespace_anywhere_stops_the_start():
    with pytest.raises(ConfigError, match="pod's own is unknown"):
        parse([{"secret": "s"}], kube=StubKube(namespace=None))


def test_a_bad_global_secret_keys_stops_the_start():
    with pytest.raises(ConfigError, match="unknown secret_keys"):
        parse([{"secret": "s"}], secret_keys={"bucket_name": "B"})


# --- single-bucket mode and the report ----------------------------------------------


def test_without_buckets_the_environment_names_the_one_bucket():
    environ = {
        "AWS_ACCESS_KEY_ID": "a",
        "AWS_SECRET_ACCESS_KEY": "s",
        "S3_BUCKET_NAME": "my-bucket",
        "AWS_DEFAULT_REGION": "us-east-1",
        "OPENSHIFT_NAMESPACE": "ns",
    }
    (spec,) = bucket_specs({}, environ=environ)

    assert spec.credentials.resolve().bucket == "my-bucket"
    assert spec.label_namespace == "ns"
    assert spec.region == "us-east-1"


def test_the_report_lists_every_bucket_and_fails_on_any():
    kube = StubKube(
        secrets={
            ("app-a", "ok"): {
                "AWS_ACCESS_KEY_ID": "AKIA-SENSITIVE",
                "AWS_SECRET_ACCESS_KEY": "x",
                "S3_BUCKET_NAME": "up",
            },
        }
    )
    specs = parse(
        [
            {"secret": "ok", "namespace": "app-a"},
            {"secret": "no", "namespace": "app-b"},
        ],
        kube=kube,
    )
    out = io.StringIO()

    status = check_config(specs, out=out)

    report = out.getvalue()
    assert status == 1
    assert "OK    secret app-a/ok: bucket up, namespace app-a" in report
    assert "FAIL  reading secret app-b/no is forbidden" in report
    assert "1 of 2 bucket(s) resolved" in report
    assert "AKIA-SENSITIVE" not in report


def test_the_report_succeeds_when_all_resolve():
    kube = StubKube(
        secrets={
            ("app-a", "ok"): {
                "AWS_ACCESS_KEY_ID": "a",
                "AWS_SECRET_ACCESS_KEY": "x",
                "S3_BUCKET_NAME": "up",
            },
        }
    )
    specs = parse([{"secret": "ok", "namespace": "app-a"}], kube=kube)

    assert check_config(specs, out=io.StringIO()) == 0


def test_unresolvable_credentials_are_not_a_config_error():
    # A missing RoleBinding fixes itself once it is applied: it must not stop
    # the start, so parsing never reads a Secret.
    specs = parse([{"secret": "later", "namespace": "app-z"}], kube=StubKube())

    with pytest.raises(CredentialsUnavailable):
        specs[0].credentials.resolve()


# --- per-bucket schedule and the mode's defaults ----------------------------------


def test_per_bucket_interval_source_and_collect():
    (spec,) = parse(
        [{"secret": "s", "interval": 120, "source": "LIST", "collect": False}]
    )

    assert spec.interval_minutes == 120
    assert spec.source == "list"
    assert spec.collect is False


def test_a_bucket_is_collected_by_default():
    (spec,) = parse([{"secret": "s"}])

    assert spec.collect is True
    assert spec.interval_minutes is None
    assert spec.source is None


@pytest.mark.parametrize(
    "entry, message",
    [
        ({"secret": "s", "interval": 0}, "interval must be"),
        ({"secret": "s", "interval": "60"}, "interval must be"),
        ({"secret": "s", "interval": True}, "interval must be"),
        ({"secret": "s", "collect": "no"}, "collect must be true or false"),
        ({"secret": "s", "source": "s3"}, "source must be one of"),
    ],
)
def test_a_bad_per_bucket_setting_stops_the_start(entry, message):
    with pytest.raises(ConfigError, match=message):
        parse([entry])


def write_config(tmp_path, *lines):
    path = tmp_path / "collector.yml"
    path.write_text(
        "kind: PrometheusExporterConfig\nconfiguration:\n" + "\n".join(lines) + "\n"
    )
    return str(path)


def test_a_bucket_list_collects_hourly_by_default(tmp_path):
    settings = load_configuration(
        write_config(tmp_path, "  - buckets:", "      - { secret: s }")
    )

    assert settings["interval"] == 60
    assert settings["max_concurrent_collections"] == 2
    assert settings["credentials_refresh"] == 300


def test_a_single_bucket_keeps_five_minutes(tmp_path):
    assert (
        load_configuration(write_config(tmp_path, "  - source: auto"))["interval"] == 5
    )


def test_an_interval_written_down_wins_in_both_modes(tmp_path):
    settings = load_configuration(
        write_config(
            tmp_path, "  - interval: 15", "  - buckets:", "      - { secret: s }"
        )
    )
    assert settings["interval"] == 15
