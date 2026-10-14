"""collector.yml read again while the exporter runs.

With a hundred buckets in one exporter, adding or fixing one must not restart
the pod: a restart re-detects every source, lists every bucket again and cuts
every probe. The file is read again on a period and on SIGHUP; when its content
changed, the bucket list is applied as a difference (see Fleet.apply).

A file that cannot be honoured keeps the configuration in force. At startup an
invalid file stops the exporter; once running, it never takes the buckets down
with it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from typing import Callable, Iterable, List, Optional, Set

from exporter.buckets import ConfigError

DEFAULT_CONFIG_RELOAD_SECONDS = 60


class ConfigReloader:
    """Reads the configuration file again and hands a new content to `apply`.

    `apply(content)` raises ConfigError to reject the content, and returns the
    settings that changed but only take effect on a restart.
    """

    def __init__(
        self,
        path: str,
        interval_seconds: float,
        apply: Callable[[bytes], Iterable[str]],
        stop: threading.Event,
        clock: Callable[[], float] = time.time,
    ):
        self.path = path
        self.interval_seconds = interval_seconds
        self._apply = apply
        self._stop = stop
        self._clock = clock
        self._wake = threading.Event()
        # What the configuration in force was read from, and the last content
        # rejected: neither is applied, nor logged, twice.
        self._applied: Optional[str] = None
        self._rejected: Optional[str] = None
        self.successful = True
        self.success_timestamp = clock()
        self.restart_required: List[str] = []

    def started_with(self, content: Optional[bytes]):
        """The content the exporter started with, so it is not applied again."""
        self._applied = _digest(content)
        self.success_timestamp = self._clock()

    def started_with_file(self):
        """The file as it is now is the configuration in force."""
        try:
            content = self._read()
        except ConfigError:
            content = None
        self.started_with(content)

    def _read(self) -> Optional[bytes]:
        """The file's content; None when there is no file."""
        try:
            with open(self.path, "rb") as config_file:
                return config_file.read()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ConfigError(f"cannot read {self.path}: {exc}") from None

    def check_once(self) -> Optional[bool]:
        """True: a new content applied; False: rejected; None: nothing new."""
        try:
            content = self._read()
        except ConfigError as exc:
            # No content to tell apart: the error itself stands for it, so an
            # unreadable file is reported once.
            key = f"error:{exc}"
            return None if key == self._rejected else self._reject(key, str(exc))

        digest = _digest(content)
        if digest == self._applied:
            if self._rejected is not None:
                # Put back as it was: what is in force is what the file says.
                logging.info("%s is back to the configuration in force", self.path)
                self._rejected = None
                self.successful = True
            return None
        if content is None:
            # Gone after the exporter started with it: a ConfigMap being
            # replaced, most likely. Not a reason to drop every bucket.
            key = "missing"
            if key == self._rejected:
                return None
            return self._reject(key, f"{self.path} is gone")
        if digest == self._rejected:
            return None
        try:
            pending = sorted(self._apply(content) or [])
        except ConfigError as exc:
            return self._reject(digest, str(exc))
        except Exception as exc:  # a bug must not end the reloads
            logging.exception("Reloading the configuration failed")
            return self._reject(digest, f"{type(exc).__name__}: {exc}")

        self._applied, self._rejected = digest, None
        self.successful = True
        self.success_timestamp = self._clock()
        if pending and pending != self.restart_required:
            logging.warning(
                "%s changed in %s: restart the exporter to apply %s",
                ", ".join(pending),
                self.path,
                "it" if len(pending) == 1 else "them",
            )
        self.restart_required = pending
        return True

    def _reject(self, digest: str, message: str) -> bool:
        self._rejected = digest
        self.successful = False
        logging.error(
            "Configuration %s rejected, keeping the one in force: %s",
            self.path,
            message,
        )
        return False

    def request(self):
        """Read the file now (SIGHUP)."""
        self._wake.set()

    def run(self):
        while not self._stop.is_set():
            # With no period, only SIGHUP (or the stop) wakes it.
            self._wake.wait(self.interval_seconds or None)
            self._wake.clear()
            if self._stop.is_set():
                return
            self.check_once()

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="config-reloader", daemon=True)
        thread.start()
        if self.interval_seconds:
            logging.info(
                "Reading %s again every %ss, and on SIGHUP",
                self.path,
                self.interval_seconds,
            )
        else:
            logging.info("Reading %s again on SIGHUP only", self.path)
        return thread

    def rows(self) -> List[list]:
        return [
            [
                ["webtech_s3", "config_last_reload_successful"],
                {},
                int(self.successful),
                "1 when the last content read from the configuration file was "
                "applied, 0 when it was rejected and the previous one kept.",
            ],
            [
                ["webtech_s3", "config_last_reload_success_timestamp_seconds"],
                {},
                self.success_timestamp,
                "When the configuration in force was applied, in Unix time.",
            ],
            [
                ["webtech_s3", "config_restart_required"],
                {},
                int(bool(self.restart_required)),
                "1 when a setting that only a restart applies changed in the "
                "configuration file.",
            ],
        ]


def _digest(content: Optional[bytes]) -> Optional[str]:
    return None if content is None else hashlib.sha256(content).hexdigest()


def restart_keys(running: dict, read: dict, reloadable: Set[str]) -> List[str]:
    """Settings that differ between the two, apart from the reloadable ones."""
    keys = (set(running) | set(read)) - reloadable
    return sorted(key for key in keys if running.get(key) != read.get(key))


def looks_mounted_with_subpath(
    path: str, mountinfo: str = "/proc/self/mountinfo"
) -> bool:
    """True when the file is a mount of its own with no ConfigMap beside it.

    A ConfigMap mounted as a directory holds a `..data` link that the kubelet
    swaps on every update. Mounted with subPath, the file is bind-mounted on
    its own and Kubernetes never updates it. Best effort: False when unsure.
    """
    try:
        real = os.path.realpath(path)
        if os.path.lexists(os.path.join(os.path.dirname(path), "..data")):
            return False
        with open(mountinfo, "r", encoding="utf-8") as mounts:
            # Field 5 is the mount point.
            return any(
                len(fields) > 4 and fields[4] == real
                for fields in (line.split() for line in mounts)
            )
    except OSError:
        return False
