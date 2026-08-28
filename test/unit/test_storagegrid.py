"""Parsing of the `x-ntap-sg-usage` payload.

Both shapes are covered: the bucket-scoped answer carries its totals at the top
level, the tenant-scoped one nests them under `buckets`.
"""

from datetime import timezone

import pytest

from exporter.sources.base import SourceUnavailable
from exporter.sources.storagegrid import is_json_response, parse_usage

BUCKET_SCOPED = {
    "calculationTime": "2026-08-28T09:22:14.421Z",
    "objectCount": 5,
    "dataBytes": 4446391,
}

TENANT_SCOPED = {
    "calculationTime": "2026-08-28T09:22:14.421Z",
    "objectCount": 7,
    "dataBytes": 4451391,
    "buckets": [
        {"name": "other", "objectCount": 2, "dataBytes": 5000},
        {"name": "my-bucket", "objectCount": 5, "dataBytes": 4446391},
    ],
}


def test_bucket_scoped_payload():
    usage = parse_usage(BUCKET_SCOPED, "my-bucket")
    assert usage.size_bytes == 4446391
    assert usage.object_count == 5


def test_tenant_scoped_payload_selects_our_bucket():
    usage = parse_usage(TENANT_SCOPED, "my-bucket")
    assert usage.size_bytes == 4446391
    assert usage.object_count == 5


def test_calculation_time_is_parsed_as_utc():
    usage = parse_usage(BUCKET_SCOPED, "my-bucket")
    assert usage.as_of.tzinfo is not None
    assert usage.as_of.astimezone(timezone.utc).year == 2026


def test_unparsable_calculation_time_keeps_the_figures():
    payload = dict(BUCKET_SCOPED, calculationTime="not-a-date")
    usage = parse_usage(payload, "my-bucket")
    assert usage.size_bytes == 4446391
    assert usage.as_of is None


def test_missing_bucket_in_tenant_payload():
    with pytest.raises(SourceUnavailable):
        parse_usage(TENANT_SCOPED, "absent")


def test_payload_without_marker_is_rejected():
    # A 200 carrying anything else means the query parameter was ignored.
    with pytest.raises(SourceUnavailable):
        parse_usage({"objectCount": 1, "dataBytes": 2}, "my-bucket")


def test_malformed_figures_are_rejected():
    with pytest.raises(SourceUnavailable):
        parse_usage(dict(BUCKET_SCOPED, dataBytes="huge"), "my-bucket")


# Verbatim shape of what a grid *without* the extension answers: HTTP 200 with
# an ordinary object listing, because the query parameter was simply ignored.
LISTING_INSTEAD_OF_USAGE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
    "<Name>bucket</Name><Prefix></Prefix><Marker></Marker>"
    "<NextMarker>1d2/abc</NextMarker><MaxKeys>1000</MaxKeys>"
    "<IsTruncated>true</IsTruncated></ListBucketResult>"
)


def test_xml_content_type_is_not_worth_reading():
    # Cuts the probe off before pulling a megabyte of object listing.
    assert is_json_response("application/xml") is False
    assert is_json_response("application/xml; charset=UTF-8") is False


def test_json_content_type_is_accepted():
    assert is_json_response("application/json") is True
    assert is_json_response("application/json; charset=UTF-8") is True


def test_missing_content_type_is_not_worth_reading():
    assert is_json_response(None) is False
    assert is_json_response("") is False


def test_a_listing_body_would_never_parse_as_usage():
    # Belt and braces: even if the content type lied, the payload is rejected.
    with pytest.raises(SourceUnavailable):
        parse_usage(LISTING_INSTEAD_OF_USAGE, "bucket")
