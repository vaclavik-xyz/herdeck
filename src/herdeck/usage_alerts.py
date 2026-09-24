"""Usage-limit alerts and pace projection over consecutive usage polls.

``UsageTracker`` is fed every poll's provider windows (with the poll's wall-clock
time) by ``usage.UsagePoller``. Per provider window it keeps:

* the current **period** (identified by its reset time; a later reset time, or
  a large usage drop when no reset time is known, starts a new one),
* the ``[usage].alert_at`` levels already announced this period,
* whether the window reached the reset level (``max(alert_at)``, or 100 when
  no levels are set) so its reset can be announced (``alert_reset``),
* a short usage history for the **pace projection**: when the recent burn
  rate fills the window before it resets, the window is annotated with
  ``full_early_s`` (seconds between the projected full time and the reset),
  which the panel's usage detail shows.

The first observation of a window is a silent baseline, like the agent
notifications in deckapp/live.py: a restart never re-announces a level that
was already crossed. A reset is detected either from the data (new period)
or from the clock (the known reset time passed) — the Claude status-line
snapshot stops updating while the user waits out a full limit, so the clock
is what makes "you can continue" arrive at all. Its latency is one poll
(``[usage].refresh_secs``).
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime

from .i18n import tr

# A reset time that moves forward by more than this starts a new period
# (smaller shifts are provider-side rounding/jitter).
_PERIOD_SHIFT_S = 5 * 60
# Without a reset time, a usage drop of more than this many points does.
_PERIOD_DROP = 10
# Pace needs at least this much history, and a projection must beat the
# reset by at least a minute to be worth a hint.
_PACE_MIN_SPAN_S = 10 * 60
_PACE_MIN_EARLY_S = 60
# Pace history horizon = a fifth of the window (5h -> 1h, 7d -> ~34h), so a
# weekly window's rate includes idle stretches instead of one busy hour.
_PACE_HORIZON_FRACTION = 0.2
_PACE_DEFAULT_WINDOW_S = 5 * 3600
_PACE_MAX_SAMPLES = 1024

_LABEL_UNITS = {"m": 60, "h": 3600, "d": 86400}


@dataclass(frozen=True)
class UsageAlert:
    kind: str  # "threshold" | "reset"
    provider: str
    window: str  # window label, e.g. "5h"
    percent: int  # the level crossed ("threshold") / reached ("reset")
    resets_at: str | None


@dataclass
class _WindowState:
    resets_at: str | None
    reset_ts: float | None
    used: int
    fired: set[int] = field(default_factory=set)
    armed: bool = False  # reached the reset level this period
    reset_announced: bool = False
    samples: deque = field(default_factory=lambda: deque(maxlen=_PACE_MAX_SAMPLES))


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _window_seconds(label: str) -> float:
    match = re.fullmatch(r"(\d+)([mhd])", label or "")
    if not match:
        return _PACE_DEFAULT_WINDOW_S
    return int(match.group(1)) * _LABEL_UNITS[match.group(2)]


class UsageTracker:
    """Stateful alert + pace engine; not thread-safe (the poller thread owns it)."""

    def __init__(self, alert_at=(), alert_reset: bool = False):
        self._levels = sorted(set(alert_at))
        self._alert_reset = bool(alert_reset)
        self._reset_level = max(self._levels, default=100)
        self._states: dict[tuple[str, str], _WindowState] = {}

    def observe(self, usages, now: float):
        """Feed one poll (``now`` = wall-clock seconds).

        Returns ``(annotated, alerts)``: the same providers with each window's
        ``full_early_s`` pace projection filled in, and the alerts to send.
        An empty ``usages`` still runs the clock-driven reset check.
        """
        alerts: list[UsageAlert] = []
        annotated = []
        for usage in usages:
            windows = []
            for window in usage.windows:
                early = self._observe_window(usage.provider, window, now, alerts)
                windows.append(replace(window, full_early_s=early))
            annotated.append(replace(usage, windows=windows))
        if self._alert_reset:
            for (provider, label), state in self._states.items():
                if (
                    state.armed
                    and not state.reset_announced
                    and state.reset_ts is not None
                    and now >= state.reset_ts
                ):
                    state.reset_announced = True
                    alerts.append(
                        UsageAlert("reset", provider, label, self._reset_level, state.resets_at)
                    )
        return annotated, alerts

    def _observe_window(self, provider, window, now, alerts) -> int | None:
        key = (provider, window.label)
        used = window.used_percent
        reset_ts = _parse_ts(window.resets_at)
        state = self._states.get(key)
        if state is None:
            # Silent baseline: whatever is already crossed stays crossed, and
            # a reset time already in the past was not witnessed by us.
            state = _WindowState(window.resets_at, reset_ts, used)
            state.fired = {level for level in self._levels if level <= used}
            state.armed = used >= self._reset_level
            state.reset_announced = reset_ts is not None and reset_ts <= now
            state.samples.append((now, used))
            self._states[key] = state
            return None
        if self._new_period(state, used, reset_ts):
            if self._alert_reset and state.armed and not state.reset_announced:
                alerts.append(
                    UsageAlert("reset", provider, window.label, self._reset_level, state.resets_at)
                )
            state = _WindowState(window.resets_at, reset_ts, used)
            self._states[key] = state
        state.resets_at, state.reset_ts, state.used = window.resets_at, reset_ts, used
        crossed = [level for level in self._levels if level <= used and level not in state.fired]
        if crossed:
            state.fired.update(crossed)
            alerts.append(
                UsageAlert("threshold", provider, window.label, max(crossed), window.resets_at)
            )
        if used >= self._reset_level:
            state.armed = True
        state.samples.append((now, used))
        return self._early_by(state, now, window.label)

    @staticmethod
    def _new_period(state: _WindowState, used: int, reset_ts: float | None) -> bool:
        if reset_ts is not None and state.reset_ts is not None:
            return reset_ts > state.reset_ts + _PERIOD_SHIFT_S
        return used + _PERIOD_DROP < state.used

    @staticmethod
    def _early_by(state: _WindowState, now: float, label: str) -> int | None:
        horizon = max(_PACE_MIN_SPAN_S, _window_seconds(label) * _PACE_HORIZON_FRACTION)
        samples = state.samples
        while samples and samples[0][0] < now - horizon:
            samples.popleft()
        if state.reset_ts is None or state.used >= 100 or len(samples) < 2:
            return None
        (t0, u0), (t1, u1) = samples[0], samples[-1]
        if t1 - t0 < _PACE_MIN_SPAN_S or u1 <= u0:
            return None
        rate = (u1 - u0) / (t1 - t0)  # percentage points per second
        full_at = t1 + (100 - u1) / rate
        early = round(state.reset_ts - full_at)
        return early if early >= _PACE_MIN_EARLY_S else None


def usage_alert_message(alert: UsageAlert, lang: str = "en", now=None) -> tuple[str, str]:
    """Localized (title, body), e.g. ("Claude 5h · 80 % used", "resets 13:05")."""
    from .layout import _fmt_reset, _provider_name

    name = _provider_name(alert.provider)
    if alert.kind == "reset":
        return (
            tr(lang, "notify.usage_reset_title", provider=name, window=alert.window),
            tr(lang, "notify.usage_reset_body"),
        )
    title = tr(
        lang, "notify.usage_threshold", provider=name, window=alert.window, pct=alert.percent
    )
    reset = _fmt_reset(alert.resets_at, now)
    body = (
        tr(lang, "notify.usage_resets_at", at=reset) if reset else tr(lang, "usage_title")
    )
    return title, body


def usage_alert_sound(notifications) -> bool | str:
    """Usage alerts are informational: they reuse the "done" event sound (and
    the `[notifications].sound` master switch) rather than adding a setting."""
    if not notifications.sound:
        return False
    return notifications.sounds.get("done", True)
