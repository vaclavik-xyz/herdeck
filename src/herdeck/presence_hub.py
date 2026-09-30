"""Bridge-side presence: is the user at ANY Mac connected to this bridge?

Every runtime reports ``{"type": "presence", "idle_s": <seconds|null>}``
(HID idle of its Mac, or since its last deck press) every PRESENCE_REPORT_S.
The hub keeps the latest report per connection and answers with the
minimum idle aged to now, so a runtime deciding [notifications.telegram]
only_when_away knows the user is typing on another Mac. Event-loop only.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

PRESENCE_CAPABILITY = "presence"
PRESENCE_REPORT_S = 30.0
# A report older than this (a runtime that stopped reporting) is ignored.
PRESENCE_STALE_S = 90.0


def clean_idle(value: object) -> float | None:
    """A reported idle time, or None when unknown/invalid."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


class PresenceHub:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._reports: dict[object, tuple[float, float | None]] = {}

    def report(self, ws, idle_s: object) -> None:
        self._reports[ws] = (self._clock(), clean_idle(idle_s))

    def drop(self, ws) -> bool:
        return self._reports.pop(ws, None) is not None

    def reporters(self) -> list:
        return list(self._reports)

    def aggregate(self) -> tuple[float | None, int]:
        """(min idle seconds now over fresh known reports | None, fresh reporters)."""
        now = self._clock()
        best: float | None = None
        fresh = 0
        for at, idle in self._reports.values():
            age = now - at
            if age > PRESENCE_STALE_S:
                continue
            fresh += 1
            if idle is not None and (best is None or idle + age < best):
                best = idle + age
        return best, fresh
