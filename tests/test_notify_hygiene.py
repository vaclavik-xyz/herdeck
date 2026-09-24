"""Notification hygiene: skip the herdr-focused pane, withdraw stale banners,
blocked reminders and away-only Telegram routing (runtime side)."""

from dataclasses import replace

from herdeck.deckapp.live import LiveSource
from herdeck.model import Status
from herdeck.presence import IdleProbe, parse_hid_idle
from tests.test_deckapp_live import RecordingNotifier, agent, notify_config


class FakeIdle:
    def __init__(self, idle):
        self.idle = idle
        self.calls = 0

    def idle_seconds(self):
        self.calls += 1
        return self.idle


def _live(config, server, *, idle=0.0, clock=None):
    probe = FakeIdle(idle)
    src = LiveSource(
        config, server, notify_schedule=lambda fn: fn(), notify_clock=clock, idle_probe=probe
    )
    notifier = RecordingNotifier()
    src._notifier = notifier
    return src, notifier, probe


# --- presence --------------------------------------------------------------


def test_hid_idle_is_parsed_from_ioreg_nanoseconds():
    out = '  |   "HIDIdleTime" = 5000000000\n  |   "HIDIdleTime" = 1\n'
    assert parse_hid_idle(out) == 5.0
    assert parse_hid_idle("no such key") is None


def test_idle_probe_caches_its_reading():
    now = [0.0]
    reads = []

    def reader():
        reads.append(now[0])
        return 30.0

    probe = IdleProbe(reader=reader, clock=lambda: now[0], ttl=15)
    assert probe.idle_seconds() == 30.0
    now[0] = 10.0
    assert probe.idle_seconds() == 40.0  # cached, aged by the elapsed time
    now[0] = 20.0
    assert probe.idle_seconds() == 30.0  # stale -> read again
    assert reads == [0.0, 20.0]
    assert IdleProbe(reader=lambda: None).idle_seconds() is None


# --- skip the herdr-focused pane ---------------------------------------------


def _focused(server, pane, status):
    return replace(agent(server.id, pane, status), focused=True)


def test_no_alert_for_the_pane_you_are_looking_at():
    config, server = notify_config()
    src, notifier, _probe = _live(config, server, idle=3.0)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    src._on_event(server.id, _focused(server, "p0", Status.BLOCKED))
    assert notifier.calls == []
    # Another (unfocused) pane still alerts.
    src._on_event(server.id, agent(server.id, "p1", Status.DONE))
    assert [c[0] for c in notifier.calls] == ["claude · done"]


def test_a_focused_pane_alerts_when_you_are_away_from_the_host():
    config, server = notify_config()
    src, notifier, _probe = _live(config, server, idle=600.0)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    src._on_event(server.id, _focused(server, "p0", Status.BLOCKED))
    assert [c[0] for c in notifier.calls] == ["claude · needs input"]


def test_unknown_idle_time_counts_as_present():
    config, server = notify_config()
    src, notifier, _probe = _live(config, server, idle=None)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    src._on_event(server.id, _focused(server, "p0", Status.DONE))
    assert notifier.calls == []


def test_skip_focused_can_be_turned_off():
    config, server = notify_config()
    config.notifications.skip_focused = False
    src, notifier, probe = _live(config, server, idle=0.0)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    src._on_event(server.id, _focused(server, "p0", Status.BLOCKED))
    assert len(notifier.calls) == 1
    assert probe.calls == 0  # no idle probe when the feature is off


def test_skip_focused_parses_default_on():
    import pytest

    from herdeck.config import ConfigError, parse_notifications
    from herdeck.settings import _notifications_config

    for parse in (parse_notifications, _notifications_config):
        assert parse({}).skip_focused is True
        assert parse({"skip_focused": False}).skip_focused is False
        with pytest.raises(ConfigError, match="notifications.skip_focused"):
            parse({"skip_focused": 0})
