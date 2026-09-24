"""Is the user at the deck host? — HID idle time, cached.

Used to route notifications: skip an alert for the herdr-focused pane only
while the user is actually at the machine, and send Telegram alerts only when
the user is away ([notifications.telegram].only_when_away).

macOS reads ``HIDIdleTime`` (nanoseconds since the last keyboard/mouse/trackpad
input) from ``ioreg -c IOHIDSystem``. Other platforms report ``None``
("unknown"); callers decide what unknown means. The probe spawns a process, so
it must only ever be called from a notify thread, never the connector event
loop; results are cached for ``ttl`` seconds so a burst of alerts costs one
``ioreg`` run.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

_HID_IDLE_RE = re.compile(r'"HIDIdleTime"\s*=\s*(\d+)')
IDLE_CACHE_TTL_S = 15.0


def parse_hid_idle(output: str) -> float | None:
    """Seconds of HID idle time from ``ioreg`` output (the first value), or None."""
    match = _HID_IDLE_RE.search(output or "")
    return int(match.group(1)) / 1e9 if match else None


def _read_macos_idle() -> float | None:
    try:
        out = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem", "-d", "4"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        log.debug("ioreg HIDIdleTime read failed", exc_info=True)
        return None
    return parse_hid_idle(out)


def _default_reader() -> Callable[[], float | None]:
    if sys.platform == "darwin":
        return _read_macos_idle
    # Linux/others: no portable, dependency-free idle source (X11/Wayland
    # differ, a headless runtime has no seat at all). Unknown.
    return lambda: None


class IdleProbe:
    """Seconds since the user's last input on this host, or None if unknown."""

    def __init__(
        self,
        *,
        reader: Callable[[], float | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = IDLE_CACHE_TTL_S,
    ):
        self._reader = reader or _default_reader()
        self._clock = clock
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cached: tuple[float, float | None] | None = None  # (read at, idle)

    def idle_seconds(self) -> float | None:
        """Blocking (spawns ``ioreg`` when the cache is stale): notify threads only."""
        now = self._clock()
        with self._lock:
            cached = self._cached
            if cached is not None and now - cached[0] < self._ttl:
                read_at, idle = cached
                # Idle time keeps growing while cached; a new input would only
                # shrink it, which the next read (<= ttl later) picks up.
                return None if idle is None else idle + (now - read_at)
            idle = self._reader()
            self._cached = (now, idle)
            return idle
