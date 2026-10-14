"""Buckets found by a label selector: the list, the annotations, the fleet."""

import base64
import io
import logging
import threading

import pytest

from exporter import check_config
from exporter.buckets import BucketPlan, ConfigError, parse_plan
from exporter.credentials import (
    CredentialsUnavailable,
    KubernetesSecretCredentials,
    ListedSecretCredentials,
    UnusableCredentials,
)
from exporter.discovery import Discoverer, SecretDiscovery, discovered_spec
from exporter.fleet import Fleet
from exporter.kube import (
    InClusterKubernetes,
    KubernetesUnavailable,
    ListedSecret,
    SecretForbidden,
    SelectorInvalid,
)
from exporter.scheduler import CredentialsRefresher

ACCESS = "AKIA-SENSITIVE-ACCESS"
SECRET = "sensitive/secret+key=="


def encoded(values):
    return {k: base64.b64encode(v.encode()).decode() for k, v in values.items()}


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    """Answers each GET with the next queued response, recording the call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, verify=None, timeout=None):
        self.calls.append({"url": url})
        return self.responses.pop(0)


ENV = {"AWS_DEFAULT_REGION": "us-east-1", "AWS_ENDPOINT_URL": "https://s3.example"}
KEYS = {"access_key": "S3_KEY", "secret_key": "S3_SECRET", "bucket": "S3_BUCKET"}
SELECTOR = "s3-exporter/watch=true"


def values(bucket, access=ACCESS):
    return {"S3_KEY": access, "S3_SECRET": SECRET, "S3_BUCKET": bucket}


def listed(bucket, annotations=None, access=ACCESS):
    return ListedSecret(values(bucket, access), dict(annotations or {}))


# --- the list call ----------------------------------------------------------------


def client(tmp_path, *responses):
    (tmp_path / "token").write_text("t0ken")
    session = FakeSession(*responses)
    return InClusterKubernetes("10.0.0.1", "443", tmp_path, session=session), session


def item(name, data, annotations=None):
    return {
        "metadata": {"name": name, "annotations": annotations or {}},
        "data": encoded(data),
    }


def test_the_list_follows_every_page_with_the_selector(tmp_path):
    kube, session = client(
        tmp_path,
        FakeResponse(
            200,
            {"items": [item("a", values("one"))], "metadata": {"continue": "next"}},
        ),
        FakeResponse(200, {"items": [item("b", values("two"), {"x/y": "z"})]}),
    )

    found = kube.list_secrets("app-a", SELECTOR)

    assert sorted(found) == ["a", "b"]
    assert found["a"].data["S3_BUCKET"] == "one"
    assert found["b"].annotations == {"x/y": "z"}
    first, second = (call["url"] for call in session.calls)
    assert first.startswith("https://10.0.0.1:443/api/v1/namespaces/app-a/secrets?")
    assert "labelSelector=s3-exporter%2Fwatch%3Dtrue" in first
    assert "limit=500" in first
    assert "continue=next" in second


def test_a_list_without_the_role_is_forbidden(tmp_path):
    kube, _ = client(tmp_path, FakeResponse(403, {"message": "nope"}))

    with pytest.raises(SecretForbidden, match="`list` on secrets"):
        kube.list_secrets("app-a", SELECTOR)


def test_a_selector_the_api_cannot_parse(tmp_path):
    kube, _ = client(
        tmp_path, FakeResponse(400, {"message": "unable to parse requirement"})
    )

    with pytest.raises(SelectorInvalid, match="unable to parse requirement"):
        kube.list_secrets("app-a", "a in (")


def test_any_other_status_is_unavailable(tmp_path):
    kube, _ = client(tmp_path, FakeResponse(500, {"message": "etcd down"}))

    with pytest.raises(KubernetesUnavailable, match="HTTP 500: etcd down"):
        kube.list_secrets("app-a", SELECTOR)


def test_one_malformed_secret_does_not_hide_the_others(tmp_path):
    broken = {"metadata": {"name": "bad"}, "data": {"S3_KEY": "%%%" + ACCESS}}
    kube, _ = client(
        tmp_path, FakeResponse(200, {"items": [broken, item("good", values("b"))]})
    )

    found = kube.list_secrets("app-a", SELECTOR)

    assert found["good"].error is None
    assert "S3_KEY is not valid base64" in found["bad"].error
    assert ACCESS not in found["bad"].error
    assert found["bad"].data == {}


def test_a_listed_secret_never_shows_its_values():
    assert ACCESS not in repr(listed("b"))


# --- the configuration ------------------------------------------------------------


class Kube:
    def __init__(self, secrets=None):
        # {(namespace, selector): {name: ListedSecret}} or an exception.
        self.secrets = secrets or {}
        self.calls = []

    def default_namespace(self):
        return "s3-exporter"

    def list_secrets(self, namespace, selector):
        self.calls.append((namespace, selector))
        answer = self.secrets.get((namespace, selector), {})
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)

    def read_secret(self, namespace, name):
        return values(name)


def plan(*entries, kube=None, **settings):
    return parse_plan(
        {"buckets": [dict(e) for e in entries], **settings}, ENV, kube or Kube()
    )


def test_a_selector_entry_becomes_one_rule_per_namespace():
    read = plan(
        {"selector": SELECTOR, "namespace": ["app-a", "app-b"], "interval": 120},
        {"secret": "named", "namespace": "app-c"},
        secret_keys={"access_key": "MY_KEY"},
    )

    assert [spec.key for spec in read.specs] == ["secret:app-c/named"]
    assert [rule.key for rule in read.rules] == [
        f"selector:app-a/{SELECTOR}",
        f"selector:app-b/{SELECTOR}",
    ]
    rule = read.rules[0]
    assert rule.interval_minutes == 120
    assert rule.keys["access_key"] == "MY_KEY"
    assert rule.endpoint_url == "https://s3.example"
    assert read.annotation_prefix == "s3-exporter"


@pytest.mark.parametrize(
    "entry, message",
    [
        ({"selector": SELECTOR}, "needs `namespace`"),
        ({"selector": SELECTOR, "namespace": []}, "empty list"),
        ({"selector": SELECTOR, "namespace": "App_A"}, "not a valid namespace"),
        ({"selector": SELECTOR, "namespace": ["a", "a"]}, "listed twice"),
        ({"selector": " ", "namespace": "a"}, "non-empty string"),
        ({"selector": SELECTOR, "namespace": "a", "name": "b"}, "bucket-name"),
        ({"selector": SELECTOR, "secret": "s", "namespace": "a"}, "exactly one"),
        ({"selector": SELECTOR, "namespace": "a", "interval": 0}, "interval"),
    ],
)
def test_a_selector_entry_a_human_must_fix(entry, message):
    with pytest.raises(ConfigError, match=message):
        plan(entry)


def test_the_same_selector_twice_in_a_namespace():
    with pytest.raises(ConfigError, match="listed twice"):
        plan(
            {"selector": SELECTOR, "namespace": "a"},
            {"selector": SELECTOR, "namespace": ["b", "a"]},
        )


def test_a_selector_needs_the_kubernetes_api():
    with pytest.raises(ConfigError, match="needs the Kubernetes API"):
        parse_plan({"buckets": [{"selector": SELECTOR, "namespace": "a"}]}, ENV, None)


@pytest.mark.parametrize("prefix", ["", "Upper", "a/b", 3, "-a"])
def test_the_annotation_prefix_must_be_a_dns_subdomain(prefix):
    with pytest.raises(ConfigError, match="annotation_prefix"):
        plan({"secret": "s"}, annotation_prefix=prefix)


def test_a_custom_annotation_prefix():
    assert plan(
        {"secret": "s"}, annotation_prefix="s3.example.com"
    ).annotation_prefix == ("s3.example.com")


# --- one discovered Secret ----------------------------------------------------------


def rule(**overrides):
    entry = {"selector": SELECTOR, "namespace": "app-a", **overrides}
    return plan(entry, secret_keys=KEYS).rules[0]


def spec_of(secret, the_rule=None, prefix="s3-exporter"):
    the_rule = the_rule or rule()
    listing = SecretDiscovery(Kube(), the_rule.namespace, the_rule.selector)
    listing.secrets = {"s": secret}
    return discovered_spec(the_rule, listing, "s", secret, prefix)


def test_a_discovered_secret_takes_the_entry_settings():
    spec = spec_of(listed("uploads"), rule(interval=30, collect=False))

    assert spec.key == "secret:app-a/s"
    assert spec.label_namespace == "app-a"
    assert spec.interval_minutes == 30
    assert spec.collect is False
    assert isinstance(spec.credentials, ListedSecretCredentials)
    assert spec.credentials.resolve().bucket == "uploads"


def test_its_annotations_override_the_entry():
    spec = spec_of(
        listed(
            "ignored",
            {
                "s3-exporter/interval": "15",
                "s3-exporter/collect": "False",
                "s3-exporter/endpoint-url": "https://other:8443",
                "s3-exporter/region": "eu-west-1",
                "s3-exporter/source": "List",
                "s3-exporter/bucket-name": "named-here",
                "other.io/ignored": "x",
            },
        ),
        rule(interval=30),
    )

    assert spec.interval_minutes == 15
    assert spec.collect is False
    assert spec.endpoint_url == "https://other:8443"
    assert spec.region == "eu-west-1"
    assert spec.source == "list"
    assert spec.credentials.resolve().bucket == "named-here"


def test_a_secret_laid_out_differently_names_its_own_keys():
    secret = ListedSecret(
        {"ak": ACCESS, "sk": SECRET, "where": "odd"},
        {
            "s3-exporter/access-key-field": "ak",
            "s3-exporter/secret-key-field": "sk",
            "s3-exporter/bucket-field": "where",
        },
    )

    credentials = spec_of(secret).credentials.resolve()

    assert (credentials.access_key, credentials.bucket) == (ACCESS, "odd")


def test_the_prefix_is_the_configured_one():
    secret = listed("b", {"s3.example.com/interval": "7", "s3-exporter/interval": "x"})

    assert spec_of(secret, prefix="s3.example.com").interval_minutes == 7


@pytest.mark.parametrize(
    "annotations, message",
    [
        ({"s3-exporter/intervall": "5"}, "unknown annotation"),
        ({"s3-exporter/interval": "soon"}, "interval must be"),
        ({"s3-exporter/interval": "-1"}, "interval must be"),
        ({"s3-exporter/collect": "maybe"}, "collect must be"),
        ({"s3-exporter/source": "magic"}, "source must be"),
        ({"s3-exporter/region": " "}, "is empty"),
    ],
)
def test_an_annotation_that_cannot_be_honoured_only_stops_that_bucket(
    annotations, message
):
    spec = spec_of(listed("b", annotations))

    assert isinstance(spec.credentials, UnusableCredentials)
    with pytest.raises(CredentialsUnavailable, match=message):
        spec.credentials.resolve()


def test_a_fixed_annotation_is_a_change_of_the_entry():
    broken = spec_of(listed("b", {"s3-exporter/interval": "soon"}))
    other = spec_of(listed("b", {"s3-exporter/interval": "later"}))
    fixed = spec_of(listed("b", {"s3-exporter/interval": "5"}))

    assert broken.fingerprint() != other.fingerprint() != fixed.fingerprint()


def test_a_secret_that_failed_to_decode_shows_why():
    spec = spec_of(ListedSecret({}, {}, "secret app-a/s: key S3_KEY is not valid"))

    with pytest.raises(CredentialsUnavailable, match="S3_KEY is not valid"):
        spec.credentials.resolve()


# --- one selector, listed again and again ----------------------------------------------


def test_a_failed_list_keeps_what_the_last_one_found(caplog):
    kube = Kube({("app-a", SELECTOR): {"s": listed("b")}})
    listing = SecretDiscovery(kube, "app-a", SELECTOR, clock=lambda: 100.0)
    listing.refresh()

    kube.secrets[("app-a", SELECTOR)] = SecretForbidden("listing is forbidden")
    with caplog.at_level(logging.DEBUG):
        listing.refresh()
        listing.refresh()

    assert list(listing.secrets) == ["s"]
    assert not listing.up
    assert listing.failures["forbidden"] == 2
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "keeping the 1 secret(s)" in errors[0].getMessage()


def test_a_successful_list_drops_what_it_no_longer_finds():
    kube = Kube({("app-a", SELECTOR): {"s": listed("b"), "t": listed("c")}})
    listing = SecretDiscovery(kube, "app-a", SELECTOR)
    listing.refresh()

    kube.secrets[("app-a", SELECTOR)] = {"t": listed("c")}
    listing.refresh()

    assert list(listing.secrets) == ["t"]
    with pytest.raises(CredentialsUnavailable, match="not found by selector"):
        listing.values("s")


def test_it_tells_which_secrets_changed_and_never_logs_a_value(caplog):
    kube = Kube({("app-a", SELECTOR): {"s": listed("b"), "t": listed("c")}})
    listing = SecretDiscovery(kube, "app-a", SELECTOR)
    with caplog.at_level(logging.DEBUG):
        assert listing.refresh() == set()

        kube.secrets[("app-a", SELECTOR)] = {
            "s": listed("b", access="ROTATED-ACCESS"),
            "t": listed("c"),
            "u": listed("d"),
        }
        assert listing.refresh() == {"s"}

    assert "new u" in caplog.text
    assert ACCESS not in caplog.text and "ROTATED-ACCESS" not in caplog.text


def test_its_metrics():
    kube = Kube({("app-a", SELECTOR): {"s": listed("b")}})
    listing = SecretDiscovery(kube, "app-a", SELECTOR, clock=lambda: 42.0)
    listing.refresh()

    rows = {(tuple(r[0]), tuple(sorted(r[1].items()))): r[2] for r in listing.rows()}
    labels = (("bucket_namespace", "app-a"), ("selector", SELECTOR))

    assert rows[(("webtech_s3", "discovery_up"), labels)] == 1
    assert rows[(("webtech_s3", "discovery_secrets"), labels)] == 1
    assert (
        rows[(("webtech_s3", "discovery_last_success_timestamp_seconds"), labels)]
        == 42.0
    )
    failures = [k for k in rows if k[0] == ("webtech_s3", "discovery_failures_total")]
    assert len(failures) == 3


# --- the fleet: named buckets plus discovered ones ---------------------------------------


class Watched:
    def __init__(self, spec):
        self.spec = spec
        self.endpoint = None
        self.refreshed = 0
        self.stopped = False
        self.needs_refresh = False

    def refresh_credentials(self):
        self.refreshed += 1

    def stop(self):
        self.stopped = True


def discoverer(kube, *entries, interval=60):
    the_plan = plan(*entries, kube=kube, secret_keys=KEYS)
    found = Discoverer(kube, the_plan, interval, threading.Event())
    found.fleet = Fleet([], Watched)
    return found


def keys(found):
    return [monitor.spec.key for monitor in found.fleet]


def test_named_buckets_and_discovered_ones_are_watched_together():
    kube = Kube({("app-a", SELECTOR): {"x": listed("1"), "y": listed("2")}})
    found = discoverer(
        kube,
        {"secret": "named", "namespace": "app-b"},
        {"selector": SELECTOR, "namespace": "app-a"},
    )

    found.run_once()

    assert keys(found) == ["secret:app-b/named", "secret:app-a/x", "secret:app-a/y"]
    assert isinstance(
        found.fleet.get("secret:app-b/named").spec.credentials,
        KubernetesSecretCredentials,
    )


def test_a_named_secret_keeps_its_entry_when_a_selector_finds_it_too(caplog):
    kube = Kube({("app-a", SELECTOR): {"x": listed("1")}})
    found = discoverer(
        kube,
        {"selector": SELECTOR, "namespace": "app-a"},
        {"secret": "x", "namespace": "app-a", "interval": 5},
    )

    with caplog.at_level(logging.INFO):
        found.run_once()
        found.run_once()

    (monitor,) = found.fleet
    assert monitor.spec.interval_minutes == 5
    assert caplog.text.count("keeps it") == 1


def test_a_secret_found_by_two_selectors_goes_to_the_first():
    kube = Kube(
        {
            ("app-a", "team=a"): {"x": listed("1")},
            ("app-a", SELECTOR): {"x": listed("1")},
        }
    )
    found = discoverer(
        kube,
        {"selector": "team=a", "namespace": "app-a", "interval": 7},
        {"selector": SELECTOR, "namespace": "app-a", "interval": 9},
    )

    found.run_once()

    (monitor,) = found.fleet
    assert monitor.spec.interval_minutes == 7


def test_secrets_come_and_go_and_an_api_outage_drops_none():
    kube = Kube({("app-a", SELECTOR): {"x": listed("1")}})
    found = discoverer(kube, {"selector": SELECTOR, "namespace": "app-a"})
    found.run_once()
    first = found.fleet.get("secret:app-a/x")

    kube.secrets[("app-a", SELECTOR)] = {"x": listed("1"), "y": listed("2")}
    found.run_once()
    assert keys(found) == ["secret:app-a/x", "secret:app-a/y"]
    assert found.fleet.get("secret:app-a/x") is first

    kube.secrets[("app-a", SELECTOR)] = KubernetesUnavailable("down")
    found.run_once()
    assert keys(found) == ["secret:app-a/x", "secret:app-a/y"]

    kube.secrets[("app-a", SELECTOR)] = {"y": listed("2")}
    found.run_once()
    assert keys(found) == ["secret:app-a/y"]
    assert first.stopped


def test_a_rotation_seen_by_the_list_is_read_at_once():
    kube = Kube({("app-a", SELECTOR): {"x": listed("1")}})
    found = discoverer(kube, {"selector": SELECTOR, "namespace": "app-a"})
    found.run_once()
    monitor = found.fleet.get("secret:app-a/x")
    before = monitor.refreshed

    kube.secrets[("app-a", SELECTOR)] = {"x": listed("1", access="NEW")}
    found.run_once()

    assert found.fleet.get("secret:app-a/x") is monitor
    assert monitor.refreshed == before + 1
    assert monitor.spec.credentials.resolve().access_key == "NEW"


def test_an_annotation_change_restarts_only_that_bucket():
    kube = Kube({("app-a", SELECTOR): {"x": listed("1"), "y": listed("2")}})
    found = discoverer(kube, {"selector": SELECTOR, "namespace": "app-a"})
    found.run_once()
    x, y = found.fleet.get("secret:app-a/x"), found.fleet.get("secret:app-a/y")

    kube.secrets[("app-a", SELECTOR)] = {
        "x": listed("1", {"s3-exporter/interval": "5"}),
        "y": listed("2"),
    }
    change = found.run_once()

    assert change.changed == ["secret:app-a/x"]
    assert x.stopped and not y.stopped


def test_a_reload_keeps_the_listing_of_an_unchanged_selector():
    kube = Kube({("app-a", SELECTOR): {"x": listed("1")}})
    found = discoverer(kube, {"selector": SELECTOR, "namespace": "app-a"})
    found.run_once()
    monitor = found.fleet.get("secret:app-a/x")
    calls = len(kube.calls)

    change = found.set_plan(
        plan(
            {"selector": SELECTOR, "namespace": "app-a"},
            {"secret": "named", "namespace": "app-b"},
            kube=kube,
            secret_keys=KEYS,
        )
    )

    assert change.added == ["secret:app-b/named"]
    assert found.fleet.get("secret:app-a/x") is monitor
    # Nothing listed: the selector was there already.
    assert len(kube.calls) == calls
    # And the monitor still reads from a listing that is kept up to date.
    kube.secrets[("app-a", SELECTOR)] = {"x": listed("1", access="NEW")}
    found.run_once()
    assert monitor.spec.credentials.resolve().access_key == "NEW"


def test_a_reload_lists_a_new_selector_at_once_and_drops_a_removed_one():
    kube = Kube(
        {
            ("app-a", SELECTOR): {"x": listed("1")},
            ("app-b", SELECTOR): {"z": listed("3")},
        }
    )
    found = discoverer(kube, {"selector": SELECTOR, "namespace": "app-a"})
    found.run_once()

    found.set_plan(
        plan({"selector": SELECTOR, "namespace": "app-b"}, kube=kube, secret_keys=KEYS)
    )

    assert keys(found) == ["secret:app-b/z"]
    assert [listing.namespace for listing in found.listings()] == ["app-b"]


def test_a_rejected_key_lists_its_namespace_once_before_the_refresh():
    kube = Kube(
        {
            ("app-a", SELECTOR): {"x": listed("1"), "y": listed("2")},
            ("app-b", SELECTOR): {"z": listed("3")},
        }
    )
    found = discoverer(kube, {"selector": SELECTOR, "namespace": ["app-a", "app-b"]})
    found.run_once()
    kube.calls.clear()

    found.relist_for(
        [found.fleet.get("secret:app-a/x"), found.fleet.get("secret:app-a/y")]
    )

    assert kube.calls == [("app-a", SELECTOR)]


def test_the_refresher_lists_before_reading_a_rejected_key_again():
    stop = threading.Event()
    calls = []
    stale = Watched(None)
    stale.needs_refresh = True
    fresh = Watched(None)
    refresher = CredentialsRefresher(
        [stale, fresh], 3600, stop, before_refresh=lambda m: calls.append(list(m))
    )
    stale.refresh_credentials = lambda: (calls.append("read"), stop.set())

    refresher.request()
    refresher.run()

    assert calls == [[stale], "read"]


def test_the_period_and_sighup():
    kube = Kube({("app-a", SELECTOR): {"x": listed("1")}})
    stop = threading.Event()
    now = [0.0]
    found = Discoverer(
        kube,
        plan({"selector": SELECTOR, "namespace": "app-a"}, kube=kube),
        0,
        stop,
        clock=lambda: now[0],
    )
    found.fleet = Fleet([], Watched)
    runs = []
    found.run_once = lambda: (runs.append(now[0]), stop.set())

    # Off: only a request lists.
    found.request()
    found.run()
    assert runs == [0.0]


def test_check_config_reports_every_selector():
    kube = Kube(
        {
            ("app-a", SELECTOR): {"x": listed("1")},
            ("app-b", SELECTOR): SecretForbidden(
                "listing secrets in app-b is forbidden"
            ),
        }
    )
    found = Discoverer(
        kube,
        plan(
            {"selector": SELECTOR, "namespace": ["app-a", "app-b"]},
            kube=kube,
            secret_keys=KEYS,
        ),
        60,
        threading.Event(),
    )
    out = io.StringIO()

    status = check_config(found.discover(), out=out, listings=found.listings())

    assert status == 1
    text = out.getvalue()
    assert f"OK    selector '{SELECTOR}' in app-a: 1 secret(s)" in text
    assert "FAIL  selector" in text and "app-b is forbidden" in text
    assert "OK    secret app-a/x: bucket 1" in text
    assert ACCESS not in text


def test_an_empty_plan_needs_no_api():
    found = Discoverer(None, BucketPlan([], []), 60, threading.Event())

    assert found.discover() == []
    assert found.rows() == []
