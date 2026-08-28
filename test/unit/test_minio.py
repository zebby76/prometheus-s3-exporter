"""Parsing of the MinIO Prometheus exposition.

The samples below are verbatim captures from MinIO RELEASE.2025-09-07 with two
4 KiB objects in `my-bucket`, taken from both the v2 and the v3 endpoints.
"""

from datetime import datetime, timezone

import pytest

from exporter.sources.base import SourceUnavailable
from exporter.sources.minio import parse_exposition, parse_usage

V2 = """
# HELP minio_bucket_usage_object_total Total number of objects
# TYPE minio_bucket_usage_object_total gauge
minio_bucket_usage_object_total{bucket="my-bucket",server="127.0.0.1:9000"} 2
# HELP minio_bucket_usage_total_bytes Total bucket size in bytes
minio_bucket_usage_total_bytes{bucket="my-bucket",server="127.0.0.1:9000"} 8192
minio_bucket_usage_last_activity_nano_seconds{server="127.0.0.1:9000"} 9.91500855e+08
"""

V3 = """
# HELP minio_cluster_usage_buckets_objects_count Total objects count in bucket
minio_cluster_usage_buckets_objects_count{bucket="my-bucket"} 2
# HELP minio_cluster_usage_buckets_total_bytes Total bucket size in bytes
minio_cluster_usage_buckets_total_bytes{bucket="my-bucket"} 8192
minio_cluster_usage_buckets_since_last_update_seconds 9.14713561e+08
"""

# What the endpoint serves before the background scanner has run: HTTP 200,
# request metrics only, no usage at all.
NO_USAGE_YET = """
# HELP minio_bucket_requests_total Total number of S3 requests on a bucket
minio_bucket_requests_total{bucket="my-bucket",server="127.0.0.1:9000"} 12
"""


def test_exposition_parser_reads_names_labels_and_values():
    samples = parse_exposition(V2)
    names = [name for name, _, _ in samples]
    assert "minio_bucket_usage_total_bytes" in names
    assert all(not name.startswith("#") for name in names)

    labels = next(
        labels
        for name, labels, _ in samples
        if name == "minio_bucket_usage_total_bytes"
    )
    assert labels["bucket"] == "my-bucket"


@pytest.mark.parametrize("exposition", [V2, V3])
def test_both_endpoint_generations_are_understood(exposition):
    usage = parse_usage(exposition, "my-bucket")
    assert usage.size_bytes == 8192
    assert usage.object_count == 2


@pytest.mark.parametrize("exposition", [V2, V3])
def test_scan_age_is_read_as_nanoseconds(exposition):
    # Both age metrics report nanoseconds on this release, including the one
    # whose name claims seconds; taken literally it would date the scan to 1996.
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    usage = parse_usage(exposition, "my-bucket", now=now)
    assert 0 <= usage.stale_seconds(now=now) < 5


def test_other_buckets_are_ignored():
    with pytest.raises(SourceUnavailable):
        parse_usage(V2, "some-other-bucket")


def test_missing_usage_metrics_is_unavailable_not_zero():
    # Reporting 0 here would look like an empty bucket instead of a scanner
    # that has not run yet.
    with pytest.raises(SourceUnavailable):
        parse_usage(NO_USAGE_YET, "my-bucket")
