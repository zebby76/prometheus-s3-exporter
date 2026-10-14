"""collector.yml read again while running: what is applied, what is kept."""

import logging
import threading

import pytest

from exporter import parse_configuration
from exporter.buckets import ConfigError
from exporter.config import (
    ConfigReloader,
    looks_mounted_with_subpath,
    restart_keys,
)

# --- strict parsing --------------------------------------------------------------


def test_an_empty_content_gives_the_defaults():
    settings = parse_configuration(b"")
    assert settings["interval"] == 5
    assert settings["config_reload_interval"] == 60


@pytest.mark.parametrize(
    "content, message",
    [
        (b"configuration: [unclosed", "not valid YAML"),
        (b"- just\n- a list\n", "must be a mapping"),
        (b"configuration: 12\n", "must be a list"),
    ],
)
def test_a_broken_content_is_an_error_not_an_empty_configuration(content, message):
    # A reload must never mistake a broken file for an empty one and drop
    # every bucket.
    with pytest.raises(ConfigError, match=message):
        parse_configuration(content)


def test_loading_at_startup_stays_lenient(tmp_path, caplog):
    from exporter import load_configuration

    broken = tmp_path / "collector.yml"
    broken.write_text("configuration: [unclosed")

    with caplog.at_level(logging.ERROR):
        settings = load_configuration(str(broken))

    assert settings["interval"] == 5
    assert "not valid YAML" in caplog.text
    assert load_configuration(str(tmp_path / "absent.yml"))["interval"] == 5


# --- what needs a restart --------------------------------------------------------


def test_only_the_bucket_settings_reload():
    running = {"interval": 60, "buckets": [1], "secret_keys": {"a": "b"}}
    read = {"interval": 30, "buckets": [1, 2], "secret_keys": {"a": "c"}}

    assert restart_keys(running, read, {"buckets", "secret_keys"}) == ["interval"]


def test_a_setting_added_or_removed_counts_too():
    assert restart_keys({"a": 1}, {"b": 1}, set()) == ["a", "b"]


# --- the reloader ----------------------------------------------------------------


class Applied:
    """Records what it is given; raises or returns what it is told to."""

    def __init__(self):
        self.contents = []
        self.error = None
        self.pending = []

    def __call__(self, content):
        if self.error:
            raise self.error
        self.contents.append(content)
        return self.pending


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "collector.yml"
    path.write_bytes(b"first")
    return path


def reloader(path, apply, clock=None):
    reloading = ConfigReloader(
        str(path), 60, apply, threading.Event(), clock=clock or (lambda: 1000.0)
    )
    reloading.started_with_file()
    return reloading


def test_an_unchanged_file_is_not_applied_again(config_file):
    apply = Applied()
    assert reloader(config_file, apply).check_once() is None
    assert apply.contents == []


def test_a_new_content_is_applied(config_file):
    apply = Applied()
    now = [1000.0]
    reloading = reloader(config_file, apply, clock=lambda: now[0])
    config_file.write_bytes(b"second")
    now[0] = 2000.0

    assert reloading.check_once() is True
    assert apply.contents == [b"second"]
    assert reloading.success_timestamp == 2000.0
    assert reloading.check_once() is None  # once


def test_a_rejected_content_keeps_the_configuration_in_force(config_file, caplog):
    apply = Applied()
    reloading = reloader(config_file, apply)
    config_file.write_bytes(b"broken")
    apply.error = ConfigError("buckets[3]: unknown key(s) secert")

    with caplog.at_level(logging.ERROR):
        assert reloading.check_once() is False
        assert reloading.check_once() is None  # logged once, not every minute

    assert not reloading.successful
    errors = [r.message for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "keeping the one in force" in errors[0]
    assert "secert" in errors[0]


def test_a_fixed_content_is_applied_and_clears_the_failure(config_file):
    apply = Applied()
    reloading = reloader(config_file, apply)
    config_file.write_bytes(b"broken")
    apply.error = ConfigError("nope")
    reloading.check_once()

    apply.error = None
    config_file.write_bytes(b"fixed")

    assert reloading.check_once() is True
    assert reloading.successful


def test_putting_the_file_back_as_it_was_clears_the_failure(config_file):
    apply = Applied()
    reloading = reloader(config_file, apply)
    config_file.write_bytes(b"broken")
    apply.error = ConfigError("nope")
    reloading.check_once()

    config_file.write_bytes(b"first")

    assert reloading.check_once() is None
    assert reloading.successful
    assert apply.contents == []


def test_a_file_that_disappears_keeps_the_buckets(config_file, caplog):
    apply = Applied()
    reloading = reloader(config_file, apply)
    config_file.unlink()

    with caplog.at_level(logging.ERROR):
        assert reloading.check_once() is False
        assert reloading.check_once() is None

    assert apply.contents == []
    assert "is gone" in caplog.text


def test_no_file_from_the_start_is_not_a_failure(tmp_path):
    apply = Applied()
    reloading = reloader(tmp_path / "absent.yml", apply)

    assert reloading.check_once() is None
    assert reloading.successful


def test_a_bug_while_applying_is_a_rejection_not_the_end_of_reloads(config_file):
    apply = Applied()
    reloading = reloader(config_file, apply)
    config_file.write_bytes(b"second")
    apply.error = RuntimeError("bug")

    assert reloading.check_once() is False

    apply.error = None
    config_file.write_bytes(b"third")
    assert reloading.check_once() is True


def test_settings_that_need_a_restart_are_said_once(config_file, caplog):
    apply = Applied()
    apply.pending = ["probe_timeout"]
    reloading = reloader(config_file, apply)
    config_file.write_bytes(b"second")

    with caplog.at_level(logging.WARNING):
        reloading.check_once()
        config_file.write_bytes(b"third")  # still pending: no new warning
        reloading.check_once()

    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [
        f"probe_timeout changed in {config_file}: restart the exporter to apply it"
    ]
    assert reloading.restart_required == ["probe_timeout"]


def rows_of(reloading):
    return {parts[-1]: value for parts, _, value, _ in reloading.rows()}


def test_the_metrics_follow_the_reloads(config_file):
    apply = Applied()
    reloading = reloader(config_file, apply)
    assert rows_of(reloading) == {
        "config_last_reload_successful": 1,
        "config_last_reload_success_timestamp_seconds": 1000.0,
        "config_restart_required": 0,
    }

    apply.pending = ["interval"]
    config_file.write_bytes(b"second")
    reloading.check_once()
    assert rows_of(reloading)["config_restart_required"] == 1

    apply.error = ConfigError("nope")
    config_file.write_bytes(b"third")
    reloading.check_once()
    assert rows_of(reloading)["config_last_reload_successful"] == 0


def test_a_request_reloads_at_once(config_file):
    apply = Applied()
    stop = threading.Event()
    reloading = ConfigReloader(str(config_file), 0, apply, stop)
    reloading.started_with_file()
    thread = reloading.start()

    config_file.write_bytes(b"second")
    reloading.request()
    for _ in range(200):
        if apply.contents:
            break
        threading.Event().wait(0.01)
    stop.set()
    reloading.request()
    thread.join(2)

    assert apply.contents == [b"second"]


# --- subPath ---------------------------------------------------------------------


def mountinfo(tmp_path, *mount_points):
    path = tmp_path / "mountinfo"
    path.write_text(
        "".join(
            f"1{i} 1 0:1 / {point} rw,relatime - overlay overlay rw\n"
            for i, point in enumerate(mount_points)
        )
    )
    return str(path)


def test_a_file_mounted_on_its_own_looks_like_subpath(tmp_path):
    config = tmp_path / "collector.yml"
    config.write_text("")

    assert looks_mounted_with_subpath(str(config), mountinfo(tmp_path, str(config)))


def test_a_configmap_directory_is_not_subpath(tmp_path):
    config = tmp_path / "collector.yml"
    config.write_text("")
    (tmp_path / "..data").symlink_to(tmp_path)

    assert not looks_mounted_with_subpath(str(config), mountinfo(tmp_path, str(config)))


def test_a_file_from_the_image_is_not_subpath(tmp_path):
    config = tmp_path / "collector.yml"
    config.write_text("")

    assert not looks_mounted_with_subpath(str(config), mountinfo(tmp_path, "/"))


def test_no_mountinfo_means_no_warning(tmp_path):
    assert not looks_mounted_with_subpath(
        str(tmp_path / "collector.yml"), str(tmp_path / "none")
    )


# --- what a new content does -----------------------------------------------------


class DiscovererStub:
    """Takes the bucket entries a reload hands over, as the Discoverer does."""

    def __init__(self):
        self.applied = []
        self.interval_seconds = 60

    def set_plan(self, plan):
        from exporter.fleet import FleetChange

        self.applied.append(plan)
        return FleetChange(added=list(plan))


MULTI = b"""
configuration:
  - interval: 60
  - buckets:
      - { env_prefix: A_ }
"""


def applier(running_content=MULTI):
    from exporter import configuration_applier, parse_configuration

    fleet = DiscovererStub()
    running = parse_configuration(running_content)
    return configuration_applier(running, fleet, plan_of=lambda s: s["buckets"]), fleet


def test_a_new_bucket_list_is_applied():
    apply, fleet = applier()

    pending = apply(MULTI.replace(b"{ env_prefix: A_ }", b"{ env_prefix: B_ }"))

    assert pending == []
    assert fleet.applied == [[{"env_prefix": "B_"}]]


def test_a_global_setting_is_reported_not_applied():
    apply, fleet = applier()

    pending = apply(MULTI.replace(b"interval: 60", b"interval: 30"))

    assert pending == ["interval"]
    assert len(fleet.applied) == 1  # the buckets still are


def test_leaving_the_list_mode_needs_a_restart():
    apply, fleet = applier()

    pending = apply(b"configuration:\n  - interval: 60\n")

    assert "buckets" in pending
    assert fleet.applied == []


def test_entering_the_list_mode_needs_a_restart():
    apply, fleet = applier(b"")

    assert "buckets" in apply(MULTI)
    assert fleet.applied == []


def test_an_invalid_entry_rejects_the_whole_file():
    from exporter import configuration_applier, parse_configuration

    fleet = DiscovererStub()
    apply = configuration_applier(parse_configuration(MULTI), fleet)

    with pytest.raises(ConfigError, match="unknown key"):
        apply(MULTI.replace(b"{ env_prefix: A_ }", b"{ env_prefix: A_, secert: x }"))
    assert fleet.applied == []
