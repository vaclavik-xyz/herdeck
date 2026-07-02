"""Poll config file mtimes and fire a callback on change.

Drives both the sidecar's in-app reload and app.py's standalone hot-reload. A
poll (not an OS watch) keeps it dependency-free and cross-platform; the interval
is short enough for an interactive editor and cheap enough to ignore.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path


class ConfigWatcher:
    def __init__(self, paths, on_change: Callable[[], None], *, interval: float = 1.0,
                 clock=time.monotonic, adopt_before_fire: bool = True):
        self._paths = [Path(p) for p in paths]
        self._on_change = on_change
        self._interval = interval
        self._clock = clock
        # adopt_before_fire=False leaves baseline adoption to the CALLBACK
        # (via dirty()/resync() under its own lock) — required when the
        # callback must distinguish a genuine external edit from a route
        # write that already reloaded and resynced (the duplicate-reload
        # race). Such a callback must resync() on handled changes, else the
        # watcher re-fires every interval while the files stay dirty.
        self._adopt_before_fire = adopt_before_fire
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="herdeck-config-watch", daemon=True)
        self._last = self._snapshot()

    def _snapshot(self) -> dict:
        out = {}
        for p in self._paths:
            try:
                out[p] = p.stat().st_mtime_ns
            except OSError:
                out[p] = None
        return out

    def start(self) -> None:
        self._thread.start()

    def dirty(self) -> bool:
        """Do the current file mtimes differ from the adopted baseline?"""
        return self._snapshot() != self._last

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            current = self._snapshot()
            if current != self._last:
                if self._adopt_before_fire:
                    self._last = current
                try:
                    self._on_change()
                except Exception:
                    pass

    def close(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def resync(self) -> None:
        """Adopt the current file mtimes as the baseline, so the next poll does not treat
        the latest (intentional) write as a change. Used by an in-process writer (the
        onboarding commit) that has already applied + swapped the change itself."""
        self._last = self._snapshot()
