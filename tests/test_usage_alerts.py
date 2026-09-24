from datetime import UTC, datetime

from herdeck.config import Notifications
from herdeck.usage import ProviderUsage, UsageWindow
from herdeck.usage_alerts import (
    UsageAlert,
    UsageTracker,
    usage_alert_message,
    usage_alert_sound,
)

T0 = 1_800_000_000.0  # wall-clock epoch seconds


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z")


def _claude(used: int, resets: float | None = T0 + 3 * 3600, label: str = "5h"):
    return ProviderUsage(
        "claude", [UsageWindow(label, used, _iso(resets) if resets is not None else None)]
    )


def _alerts(tracker, usage, now):
    return tracker.observe([usage] if usage is not None else [], now)[1]


# --- thresholds -------------------------------------------------------------


def test_first_poll_is_a_silent_baseline():
    tracker = UsageTracker(alert_at=[50, 80], alert_reset=True)
    assert _alerts(tracker, _claude(90), T0) == []
    # Already-crossed levels stay quiet for the rest of the period.
    assert _alerts(tracker, _claude(92), T0 + 300) == []


def test_threshold_fires_once_per_period_on_upward_cross():
    tracker = UsageTracker(alert_at=[80, 95])
    _alerts(tracker, _claude(70), T0)
    alerts = _alerts(tracker, _claude(81), T0 + 300)
    assert alerts == [UsageAlert("threshold", "claude", "5h", 80, _iso(T0 + 3 * 3600))]
    assert _alerts(tracker, _claude(84), T0 + 600) == []
    assert _alerts(tracker, _claude(79), T0 + 900) == []  # same period: no re-arm
    assert _alerts(tracker, _claude(82), T0 + 1200) == []


def test_jump_over_several_levels_sends_only_the_highest():
    tracker = UsageTracker(alert_at=[50, 80, 95])
    _alerts(tracker, _claude(10), T0)
    alerts = _alerts(tracker, _claude(97), T0 + 300)
    assert [a.percent for a in alerts] == [95]
    assert _alerts(tracker, _claude(99), T0 + 600) == []


def test_thresholds_rearm_when_the_window_resets():
    tracker = UsageTracker(alert_at=[80])
    _alerts(tracker, _claude(70), T0)
    assert len(_alerts(tracker, _claude(85), T0 + 300)) == 1
    # New period: the reset time moved forward by a full window.
    later = T0 + 8 * 3600
    assert _alerts(tracker, _claude(5, resets=later), T0 + 4 * 3600) == []
    assert [a.percent for a in _alerts(tracker, _claude(80, resets=later), T0 + 5 * 3600)] == [80]


def test_windows_are_tracked_independently():
    tracker = UsageTracker(alert_at=[80])
    both = lambda five, week: ProviderUsage(  # noqa: E731
        "claude",
        [UsageWindow("5h", five, _iso(T0 + 3600)), UsageWindow("7d", week, _iso(T0 + 86400))],
    )
    tracker.observe([both(10, 70)], T0)
    alerts = tracker.observe([both(20, 85)], T0 + 300)[1]
    assert [(a.window, a.percent) for a in alerts] == [("7d", 80)]


def test_no_thresholds_means_no_threshold_alerts():
    tracker = UsageTracker()
    _alerts(tracker, _claude(10), T0)
    assert _alerts(tracker, _claude(100), T0 + 300) == []


# --- reset ------------------------------------------------------------------


def test_reset_fires_when_the_reset_time_passes_after_hitting_100():
    tracker = UsageTracker(alert_reset=True)
    resets = T0 + 3600
    _alerts(tracker, _claude(90, resets=resets), T0)
    _alerts(tracker, _claude(100, resets=resets), T0 + 300)
    # The status-line cache stays stale (the user stopped): time alone decides.
    assert _alerts(tracker, None, resets - 1) == []
    alerts = _alerts(tracker, _claude(100, resets=resets), resets + 5)
    assert alerts == [UsageAlert("reset", "claude", "5h", 100, _iso(resets))]
    assert _alerts(tracker, _claude(100, resets=resets), resets + 300) == []
    # The fresh post-reset data does not announce the same reset twice.
    assert _alerts(tracker, _claude(2, resets=resets + 5 * 3600), resets + 600) == []


def test_reset_level_is_the_highest_threshold():
    tracker = UsageTracker(alert_at=[80, 90], alert_reset=True)
    resets = T0 + 3600
    _alerts(tracker, _claude(10, resets=resets), T0)
    _alerts(tracker, _claude(91, resets=resets), T0 + 300)  # threshold alert
    assert [a.kind for a in _alerts(tracker, None, resets + 1)] == ["reset"]


def test_reset_needs_the_level_reached():
    tracker = UsageTracker(alert_reset=True)
    resets = T0 + 3600
    _alerts(tracker, _claude(10, resets=resets), T0)
    _alerts(tracker, _claude(60, resets=resets), T0 + 300)
    assert _alerts(tracker, None, resets + 1) == []


def test_reset_fires_on_new_period_before_the_advertised_time():
    tracker = UsageTracker(alert_reset=True)
    _alerts(tracker, _claude(10), T0)
    _alerts(tracker, _claude(100), T0 + 300)
    alerts = _alerts(tracker, _claude(1, resets=T0 + 9 * 3600), T0 + 600)
    assert [a.kind for a in alerts] == ["reset"]


def test_reset_disabled_by_default():
    tracker = UsageTracker(alert_at=[100])
    resets = T0 + 3600
    _alerts(tracker, _claude(10, resets=resets), T0)
    assert len(_alerts(tracker, _claude(100, resets=resets), T0 + 300)) == 1
    assert _alerts(tracker, None, resets + 1) == []


def test_startup_on_an_already_reset_window_stays_quiet():
    tracker = UsageTracker(alert_reset=True)
    # Stale 100 % snapshot whose reset time already passed before startup.
    assert _alerts(tracker, _claude(100, resets=T0 - 60), T0) == []
    assert _alerts(tracker, _claude(100, resets=T0 - 60), T0 + 300) == []


def test_startup_at_100_announces_the_later_reset():
    tracker = UsageTracker(alert_reset=True)
    resets = T0 + 3600
    assert _alerts(tracker, _claude(100, resets=resets), T0) == []
    assert [a.kind for a in _alerts(tracker, None, resets + 1)] == ["reset"]


def test_period_without_reset_time_uses_a_usage_drop():
    tracker = UsageTracker(alert_at=[80], alert_reset=True)
    _alerts(tracker, _claude(70, resets=None), T0)
    assert len(_alerts(tracker, _claude(100, resets=None), T0 + 300)) == 1
    alerts = _alerts(tracker, _claude(3, resets=None), T0 + 600)
    assert [a.kind for a in alerts] == ["reset"]
    assert [a.percent for a in _alerts(tracker, _claude(85, resets=None), T0 + 900)] == [80]


# --- pace projection ----------------------------------------------------------


def _pace(tracker, used, now, resets=T0 + 3 * 3600):
    annotated = tracker.observe([_claude(used, resets=resets)], now)[0]
    return annotated[0].windows[0].full_early_s


def test_pace_needs_ten_minutes_of_signal():
    tracker = UsageTracker()
    assert _pace(tracker, 10, T0) is None
    assert _pace(tracker, 30, T0 + 300) is None  # 5 minutes: not enough


def test_pace_projects_full_before_reset():
    tracker = UsageTracker()
    _pace(tracker, 10, T0)
    # +20 points in 20 minutes = 1 %/min -> the remaining 70 % fill in 70 min,
    # i.e. at T0+90min, while the window resets at T0+180min.
    early = _pace(tracker, 30, T0 + 1200)
    assert early == 90 * 60


def test_pace_stays_quiet_when_the_window_resets_first():
    tracker = UsageTracker()
    _pace(tracker, 10, T0)
    assert _pace(tracker, 12, T0 + 1200) is None  # 0.1 %/min: full long after reset


def test_pace_ignores_flat_or_falling_usage_and_full_windows():
    tracker = UsageTracker()
    _pace(tracker, 40, T0)
    assert _pace(tracker, 40, T0 + 1200) is None
    tracker = UsageTracker()
    _pace(tracker, 50, T0)
    assert _pace(tracker, 100, T0 + 1200) is None


def test_pace_uses_recent_history_only():
    tracker = UsageTracker()
    # A fast burst long ago, then flat for over an hour (5h window: 60 min horizon).
    _pace(tracker, 0, T0)
    _pace(tracker, 50, T0 + 600)
    for minute in range(20, 90, 5):
        last = _pace(tracker, 50, T0 + minute * 60)
    assert last is None


def test_pace_restarts_with_a_new_period():
    tracker = UsageTracker()
    _pace(tracker, 10, T0)
    _pace(tracker, 60, T0 + 1200)
    later = T0 + 8 * 3600
    assert _pace(tracker, 1, T0 + 4 * 3600, resets=later) is None


# --- messages -----------------------------------------------------------------


def test_threshold_message_en_and_cs():
    alert = UsageAlert("threshold", "claude", "5h", 80, None)
    assert usage_alert_message(alert, "en")[0] == "Claude 5h · 80 % used"
    assert usage_alert_message(alert, "cs")[0] == "Claude 5h · využito 80 %"


def test_threshold_message_body_carries_reset_time():
    resets = datetime(2026, 9, 24, 13, 5).astimezone()
    alert = UsageAlert("threshold", "codex", "7d", 95, resets.isoformat())
    now = resets.replace(hour=9)
    assert usage_alert_message(alert, "en", now=now)[1] == "resets 13:05"
    assert usage_alert_message(alert, "cs", now=now)[1] == "obnova 13:05"


def test_reset_message_en_and_cs():
    alert = UsageAlert("reset", "claude", "5h", 100, None)
    assert usage_alert_message(alert, "en") == ("Claude 5h reset", "you can continue")
    assert usage_alert_message(alert, "cs") == ("Claude 5h obnoveno", "můžeš pokračovat")


def test_usage_alert_sound_follows_the_done_sound():
    n = Notifications()
    assert usage_alert_sound(n) == n.sounds["done"]
    n.sound = False
    assert usage_alert_sound(n) is False
