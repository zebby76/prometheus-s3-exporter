"""Source selection: detection order, explicit locking, and the fallback."""

import pytest

from exporter import sources
from exporter.sources import SourceContext, Usage, resolve
from exporter.sources.base import UsageSource
from exporter.sources.listing import ListingSource


def make_context(**overrides):
    defaults = dict(
        endpoint_url="http://minio:9000",
        region="us-east-1",
        bucket="my-bucket",
        access_key="ak",
        secret_key="sk",
        client=object(),
        config={},
    )
    defaults.update(overrides)
    return SourceContext(**defaults)


class FakeSource(UsageSource):
    """A source whose availability the test decides."""

    name = "fake"
    available = True

    @classmethod
    def probe(cls, ctx):
        return cls() if cls.available else None

    def fetch(self):
        return Usage(size_bytes=1, object_count=1)


@pytest.fixture
def restore_sources(monkeypatch):
    yield monkeypatch


def test_auto_prefers_the_first_available_source(monkeypatch):
    class First(FakeSource):
        name = "first"

    class Second(FakeSource):
        name = "second"

    monkeypatch.setattr(sources, "SOURCES", (First, Second))
    assert resolve(make_context(), "auto").name == "first"


def test_auto_skips_unavailable_sources(monkeypatch):
    class Unavailable(FakeSource):
        name = "unavailable"
        available = False

    class Usable(FakeSource):
        name = "usable"

    monkeypatch.setattr(sources, "SOURCES", (Unavailable, Usable))
    assert resolve(make_context(), "auto").name == "usable"


def test_explicit_source_short_circuits_detection(monkeypatch):
    class Cheap(FakeSource):
        name = "cheap"

    class Preferred(FakeSource):
        name = "preferred"

    monkeypatch.setattr(sources, "SOURCES", (Cheap, Preferred))
    assert resolve(make_context(), "preferred").name == "preferred"


def test_explicit_but_unavailable_source_falls_back_to_listing(monkeypatch):
    class Broken(FakeSource):
        name = "broken"
        available = False

    monkeypatch.setattr(sources, "SOURCES", (Broken,))
    # Degrading beats serving nothing; the substitution shows up in the
    # exporter_info source label.
    assert isinstance(resolve(make_context(), "broken"), ListingSource)


def test_unknown_source_name_falls_back_to_detection(monkeypatch):
    class Usable(FakeSource):
        name = "usable"

    monkeypatch.setattr(sources, "SOURCES", (Usable,))
    assert resolve(make_context(), "does-not-exist").name == "usable"


def test_listing_probe_always_succeeds():
    # Detection can never come up empty because of this.
    assert ListingSource.probe(make_context()) is not None


def test_source_config_returns_empty_mapping_when_absent():
    assert make_context().source_config("minio") == {}


def test_source_config_rejects_a_non_mapping():
    ctx = make_context(config={"minio": "http://example"})
    with pytest.raises(ValueError):
        ctx.source_config("minio")
