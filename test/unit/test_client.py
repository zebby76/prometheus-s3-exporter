"""S3 clients: the collection defaults, and the probe's own budget."""

import pytest

import exporter
from exporter import (
    DEFAULT_PROBE_TIMEOUT_SECONDS,
    build_probe_client,
    build_s3_client,
    load_configuration,
    probe_client_for,
    probe_timeout_for,
)

CREDENTIALS = ("key", "secret", "eu-west-1", "https://s3.example.invalid")


def test_the_collection_client_keeps_the_boto3_defaults():
    # A transient error in the middle of 150 pages should be retried, not fail
    # the hour, so this client is deliberately left alone.
    client = build_s3_client(*CREDENTIALS)

    assert client.meta.config.connect_timeout == 60
    assert client.meta.config.read_timeout == 60


def test_the_probe_client_fails_fast_and_never_retries():
    client = build_probe_client(*CREDENTIALS, 5)
    config = client.meta.config

    assert config.connect_timeout == 5
    assert config.read_timeout == 5
    # botocore resolves Config(max_attempts=N) to total_max_attempts=N+1, and
    # it is the resolved value that governs: 1 means one round trip, with no
    # hidden backoff folded into the measured duration.
    assert config.retries["total_max_attempts"] == 1
    assert config.retries["mode"] == "standard"


def test_the_probe_client_keeps_its_connection_alive():
    # Between two hourly collections the probe is the only traffic on this
    # connection; an idle minute is enough for the far side to drop it.
    assert build_probe_client(*CREDENTIALS, 5).meta.config.tcp_keepalive is True


@pytest.mark.parametrize(
    "configured,interval,expected",
    [
        (5, 60, 5),
        # A whole check is two attempts of connect plus read: four timeouts.
        (30, 60, 15),
        (5, 5, 1),
        (60, 10, 2),
        # Never zero: a zero timeout means "no timeout" to botocore.
        (5, 1, 1),
        # No interval means no probe, so nothing to clamp against.
        (5, 0, 5),
    ],
)
def test_the_timeout_fits_inside_the_interval(configured, interval, expected):
    assert probe_timeout_for(configured, interval) == expected


def test_probe_timeout_has_a_default(tmp_path):
    settings = load_configuration(path=str(tmp_path / "absent.yml"))

    assert settings["probe_timeout"] == DEFAULT_PROBE_TIMEOUT_SECONDS


def test_probe_timeout_is_read_from_the_configuration(tmp_path):
    config = tmp_path / "collector.yml"
    config.write_text("configuration:\n  - probe_timeout: 12\n", encoding="utf-8")

    assert load_configuration(path=str(config))["probe_timeout"] == 12


SHARED = object()


def probe_client(interval, **kwargs):
    return probe_client_for(SHARED, interval, 5, *CREDENTIALS, **kwargs)


def test_no_probe_client_is_built_when_the_probe_is_off():
    # connectivity_interval: 0 must cost nothing at all -- no second client,
    # no second connection pool.
    assert probe_client(0) is SHARED


def test_the_probe_gets_a_client_of_its_own_when_enabled():
    client = probe_client(60)

    assert client is not SHARED
    assert client.meta.config.retries["total_max_attempts"] == 1


def test_a_probe_client_that_cannot_be_built_falls_back_to_the_shared_one(monkeypatch):
    # The probe is auxiliary: it must not stop the exporter from starting.
    def boom(*_args, **_kwargs):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(exporter, "build_probe_client", boom)

    assert probe_client(60) is SHARED
