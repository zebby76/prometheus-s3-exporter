"""Cloudflare R2 usage API: parsing, staleness, and the opt-in probe.

The payloads below are verbatim captures from the account API (August 2026), one
per jurisdiction. Note that every figure is a decimal STRING, and that the
response carries the `end` of the aggregation window it describes.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from exporter.sources import SourceContext
from exporter.sources.base import SourceUnavailable
from exporter.sources.r2 import R2Source, parse_usage

DEFAULT_JURISDICTION = json.loads(
    """
{"success": true, "errors": [], "messages": [],
 "result": {"end": "2026-08-29T19:20:00.000Z",
            "payloadSize": "311465243", "metadataSize": "1101125",
            "objectCount": "18987", "uploadCount": "0",
            "infrequentAccessPayloadSize": "0", "infrequentAccessMetadataSize": "0",
            "infrequentAccessObjectCount": "0", "infrequentAccessUploadCount": "0"}}
"""
)

EU_JURISDICTION = json.loads(
    """
{"success": true, "errors": [], "messages": [],
 "result": {"end": "2026-08-29T19:40:00.000Z",
            "payloadSize": "418426620", "metadataSize": "57466",
            "objectCount": "974", "uploadCount": "0",
            "infrequentAccessPayloadSize": "0", "infrequentAccessMetadataSize": "0",
            "infrequentAccessObjectCount": "0", "infrequentAccessUploadCount": "0"}}
"""
)

# What the API answers for a token without Workers R2 Storage: Read.
REFUSED = json.loads(
    """
{"success": false, "messages": [],
 "errors": [{"code": 10000, "message": "Authentication error"}], "result": null}
"""
)


def make_context(**overrides):
    defaults = dict(
        endpoint_url="https://acct.r2.cloudflarestorage.com",
        region="auto",
        bucket="my-bucket",
        access_key="ak",
        secret_key="sk",
        client=object(),
        config={},
    )
    defaults.update(overrides)
    return SourceContext(**defaults)


def test_parses_the_figures_the_api_returns_as_strings():
    usage = parse_usage(DEFAULT_JURISDICTION, "my-bucket")
    assert usage.size_bytes == 311465243
    assert usage.object_count == 18987


def test_metadata_size_is_excluded_to_stay_comparable_with_the_listing():
    # The listing sums object payloads only; counting Cloudflare's metadata here
    # would make the two sources disagree by design on the very same bucket.
    assert parse_usage(DEFAULT_JURISDICTION, "my-bucket").size_bytes == 311465243


def test_infrequent_access_objects_are_counted_too():
    document = json.loads(json.dumps(DEFAULT_JURISDICTION))
    document["result"]["infrequentAccessPayloadSize"] = "1000"
    document["result"]["infrequentAccessObjectCount"] = "7"

    usage = parse_usage(document, "my-bucket")
    assert usage.size_bytes == 311465243 + 1000
    assert usage.object_count == 18987 + 7


def test_staleness_comes_from_the_aggregation_window():
    usage = parse_usage(EU_JURISDICTION, "my-eu-bucket")
    assert usage.as_of == datetime(2026, 8, 29, 19, 40, tzinfo=timezone.utc)

    now = usage.as_of + timedelta(minutes=40)
    assert usage.stale_seconds(now) == pytest.approx(2400)


def test_a_missing_window_is_reported_as_live_rather_than_guessed():
    document = json.loads(json.dumps(DEFAULT_JURISDICTION))
    del document["result"]["end"]
    assert parse_usage(document, "my-bucket").as_of is None


def test_an_unparsable_window_does_not_break_the_reading():
    document = json.loads(json.dumps(DEFAULT_JURISDICTION))
    document["result"]["end"] = "not a date"
    assert parse_usage(document, "my-bucket").as_of is None


def test_a_refused_call_names_the_reason():
    with pytest.raises(SourceUnavailable, match="Authentication error"):
        parse_usage(REFUSED, "my-bucket")


def test_a_success_without_figures_is_unavailable():
    with pytest.raises(SourceUnavailable, match="no figures"):
        parse_usage({"success": True, "result": None}, "my-bucket")


def test_a_non_numeric_figure_is_unavailable():
    document = json.loads(json.dumps(DEFAULT_JURISDICTION))
    document["result"]["objectCount"] = "many"
    with pytest.raises(SourceUnavailable, match="objectCount"):
        parse_usage(document, "my-bucket")


def test_probe_stays_silent_when_nothing_is_configured(monkeypatch):
    # Opt-in: the account API needs a credential the exporter is not given by
    # default, so an unconfigured deployment must fall through to the listing.
    monkeypatch.delenv("CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CF_API_TOKEN", raising=False)
    assert R2Source.probe(make_context()) is None


def test_probe_reads_the_environment_when_the_config_is_empty(monkeypatch):
    monkeypatch.setenv("CF_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CF_API_TOKEN", "token")
    monkeypatch.setattr(R2Source, "fetch", lambda self: None)

    source = R2Source.probe(make_context())
    assert source is not None
    assert source.name == "r2"


def test_the_jurisdiction_header_is_sent_only_when_asked(monkeypatch):
    ctx = make_context(bucket="my-eu-bucket")
    plain = R2Source(ctx, "acct", "token")
    assert "cf-r2-jurisdiction" not in plain._headers

    # A bucket created in a jurisdiction does not exist on the default endpoint:
    # without the header the API answers 404 rather than a wrong figure.
    scoped = R2Source(ctx, "acct", "token", jurisdiction="eu")
    assert scoped._headers["cf-r2-jurisdiction"] == "eu"
    assert scoped._url.endswith("/r2/buckets/my-eu-bucket/usage")
