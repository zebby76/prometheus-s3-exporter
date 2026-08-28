"""The name/label sanitising the collector relies on."""

from exporter import (
    format_label_key,
    format_label_value,
    format_metric_name,
    group_metrics,
)
from exporter.utils import merge_dicts_ordered


def test_metric_name_is_lowercased_and_joined():
    assert format_metric_name(["webtech_s3", "bucket_size_bytes"]) == (
        "webtech_s3_bucket_size_bytes"
    )


def test_metric_name_replaces_invalid_characters():
    assert format_metric_name(["web-tech", "size!"]) == "web_tech_size_"


def test_metric_name_cannot_start_with_a_digit():
    assert format_metric_name(["1st"]).startswith("_")


def test_label_key_leading_double_underscore_is_collapsed():
    # Prometheus reserves the __ prefix for its own labels.
    assert format_label_key("__meta") == "_meta"


def test_label_value_joins_lists():
    assert format_label_value(["a", 1]) == "a_1"
    assert format_label_value(3) == "3"


def test_group_metrics_keys_by_label_values():
    grouped = group_metrics(
        [
            [["webtech_s3", "size"], {"name": "a"}, 1, "desc"],
            [["webtech_s3", "size"], {"name": "b"}, 2, "desc"],
        ]
    )
    label_keys, values, description = grouped["webtech_s3_size"]
    assert label_keys == ("name",)
    assert values == {("a",): 1, ("b",): 2}
    assert description == {"description": "desc"}


def test_merge_dicts_ordered_last_wins_and_keeps_order():
    merged = merge_dicts_ordered({"a": 1, "b": 2}, {"b": 3}, c=4)
    assert list(merged.items()) == [("a", 1), ("b", 3), ("c", 4)]
