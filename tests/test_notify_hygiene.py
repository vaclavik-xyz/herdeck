"""Notification hygiene: skip the herdr-focused pane, withdraw stale banners,
blocked reminders and away-only Telegram routing (runtime side)."""

from dataclasses import replace

from herdeck.config import TelegramConfig
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


def test_unknown_idle_time_counts_as_away():
    # No idle reading (e.g. Linux) is no proof the user sits at the host, so
    # the alert is not dropped.
    config, server = notify_config()
    src, notifier, _probe = _live(config, server, idle=None)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    src._on_event(server.id, _focused(server, "p0", Status.DONE))
    assert [c[0] for c in notifier.calls] == ["claude · done"]


def test_a_focused_pane_still_reaches_remote_backends(monkeypatch):
    """skip_focused silences only the local banner: Telegram (one-way and
    interactive) is for when you are away, and presence is a guess."""
    import herdeck.notify as notify

    config, server = notify_config(on=("blocked", "done"))
    config.notifications.backends = ["macos", "telegram"]
    config.notifications.telegram = TelegramConfig(token_env="TG_TOKEN", chat_id="-1")
    monkeypatch.setenv("TG_TOKEN", "bot")
    local, remote = [], []
    monkeypatch.setattr(
        notify,
        "make_telegram_sink",
        lambda token, chat, thread: lambda title, body, sound, icon=None: remote.append(title),
    )
    src = LiveSource(
        config,
        server,
        notify_schedule=lambda fn: fn(),
        idle_probe=FakeIdle(3.0),  # present
        shell_banners=False,
    )
    # the same sink LiveSource builds, with a recording local banner
    src._notifier = notify.Notifier(
        sink=notify.deckapp_sink(
            src._notify_feed,
            lambda: False,
            config,
            telegram_factory=src._telegram_sink,
            macos_sink=lambda title, body, sound, icon=None: local.append(title),
            shell=False,
            local_gate=lambda: not getattr(src._alert_context, "skip_local", False),
        )
    )
    interactive = []
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    src._on_event(server.id, _focused(server, "p0", Status.DONE))
    assert local == [] and remote == ["claude · done"]
    src.set_telegram_interactive(lambda: True, lambda a, **k: interactive.append(a.key))
    src._on_event(server.id, _focused(server, "p0", Status.BLOCKED))
    assert local == [] and interactive == [agent(server.id, "p0", Status.BLOCKED).key]
    # an unfocused pane keeps its local banner
    src._on_event(server.id, agent(server.id, "p1", Status.DONE))
    assert local == ["claude · done"]


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


# --- withdraw stale banners ---------------------------------------------------


def _feed_live(features=frozenset({"withdraw"}), *, clock=None):
    from herdeck.notify import runtime_sink

    config, server = notify_config()
    src = LiveSource(
        config,
        server,
        notify_schedule=lambda fn: fn(),
        notify_clock=clock,
        notify_sink_factory=lambda feed, gate: runtime_sink(feed, gate, fallback=lambda *a: None),
        idle_probe=FakeIdle(0.0),
    )
    src.set_notify_gate(lambda: True, features=lambda: features)
    return src, server


def _kinds(src):
    return [(i["kind"], i.get("agent", {}).get("pane_id")) for i in src._notify_feed.state()["items"]]


def test_answered_blocked_agent_withdraws_its_banner():
    src, server = _feed_live()
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    src._on_event(server.id, agent(server.id, "p0", Status.BLOCKED))
    src._on_event(server.id, agent(server.id, "p0", Status.WORKING))  # answered anywhere
    assert _kinds(src) == [("alert", "p0"), ("withdraw", "p0")]
    # Nothing left to withdraw: a second change queues nothing.
    src._on_event(server.id, agent(server.id, "p0", Status.IDLE))
    assert len(_kinds(src)) == 2


def test_done_agent_working_again_withdraws_and_a_new_block_alerts_after_it():
    now = [100.0]
    src, server = _feed_live(clock=lambda: now[0])
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    src._on_event(server.id, agent(server.id, "p0", Status.DONE))
    src._on_event(server.id, agent(server.id, "p0", Status.BLOCKED))
    assert _kinds(src) == [("alert", "p0"), ("withdraw", "p0"), ("alert", "p0")]


def test_a_vanished_agent_withdraws_its_banner():
    src, server = _feed_live()
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    src._on_snapshot(server.id, [])
    assert _kinds(src)[-1] == ("withdraw", "p0")


def test_no_withdraw_without_a_banner_or_for_an_old_shell():
    src, server = _feed_live()
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])  # baseline, no alert
    src._on_event(server.id, agent(server.id, "p0", Status.WORKING))
    assert _kinds(src) == []

    old, server = _feed_live(features=frozenset())
    old._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    old._on_event(server.id, agent(server.id, "p0", Status.BLOCKED))
    old._on_event(server.id, agent(server.id, "p0", Status.WORKING))
    assert _kinds(old) == [("alert", "p0")]  # would show as an empty banner


def test_withdraw_fallback_is_acked_without_an_osascript_banner():
    from herdeck.notify import NotificationFeed

    feed = NotificationFeed()
    item = feed.withdraw({"server_id": "s", "pane_id": "p"})
    delivered = []
    assert feed.fallback(item["generation"], item["seq"], lambda *a: delivered.append(a))
    assert delivered == []
    assert feed.state()["acked_seq"] == item["seq"]


def test_shell_features_header_reaches_the_source():
    from herdeck.deckapp import DeckApp
    from tests.test_deckapp_live import StubIcons, live_config

    config, server = live_config()
    src = LiveSource(config, server)
    app = DeckApp(src, serve=False, icon_provider=StubIcons())
    try:
        app._wire_notify_gate(src)
        assert src._notify_features() == frozenset()
        app.note_shell_features("withdraw, future-thing,")
        assert src._notify_features() == frozenset({"withdraw", "future-thing"})
    finally:
        app.close()


# --- reminders ------------------------------------------------------------------


def _reminding(remind_after=10, lang="en"):
    config, server = notify_config()
    config.notifications.remind_after = remind_after
    config.view.language = lang
    now = [1000.0]
    src, notifier, _probe = _live(config, server, clock=lambda: now[0])
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    src._on_event(server.id, agent(server.id, "p0", Status.BLOCKED))
    return src, server, notifier, now


def test_a_still_blocked_agent_is_reminded_once_per_interval_at_most_three_times():
    src, server, notifier, now = _reminding()
    try:
        assert len(notifier.calls) == 1
        now[0] += 9 * 60
        assert src.check_reminders() == 0
        now[0] += 60
        assert src.check_reminders() == 1
        assert notifier.calls[-1][0] == "claude · still needs input (10 min)"
        assert notifier.metas[-1]["episode"] == src._block_episode[agent(server.id, "p0", Status.BLOCKED).key]
        assert src.check_reminders() == 0  # once per interval
        for minutes in (20, 30):
            now[0] = 1000.0 + minutes * 60
            assert src.check_reminders() == 1
        now[0] = 1000.0 + 40 * 60
        assert src.check_reminders() == 0  # max three per episode
        assert len(notifier.calls) == 4
    finally:
        src.close()


def test_no_reminder_once_the_agent_left_the_episode():
    src, server, notifier, now = _reminding()
    try:
        src._on_event(server.id, agent(server.id, "p0", Status.WORKING))
        src._on_event(server.id, agent(server.id, "p0", Status.BLOCKED))  # new episode
        now[0] += 10 * 60
        # Only the NEW episode (begun at the same clock time here) reminds.
        assert src.check_reminders() == 1
        src._on_event(server.id, agent(server.id, "p0", Status.WORKING))
        now[0] += 10 * 60
        assert src.check_reminders() == 0
        assert src._reminders == {}
    finally:
        src.close()


def test_reminder_title_is_localized_and_off_by_default():
    src, _server, notifier, now = _reminding(lang="cs")
    try:
        now[0] += 25 * 60
        src.check_reminders()
        assert notifier.calls[-1][0] == "claude · pořád čeká na tebe (25 min)"
    finally:
        src.close()
    off, _server, notifier, now = _reminding(remind_after=0)
    now[0] += 60 * 60
    assert off.check_reminders() == 0
    assert off._reminder_thread is None


def test_remind_after_validates_minutes():
    import pytest

    from herdeck.config import ConfigError, parse_notifications
    from herdeck.settings import _notifications_config

    for parse in (parse_notifications, _notifications_config):
        assert parse({}).remind_after == 0
        assert parse({"remind_after": 15}).remind_after == 15
        for bad in (-1, 1.5, "10", True, 2000):
            with pytest.raises(ConfigError, match="notifications.remind_after"):
                parse({"remind_after": bad})


# --- Telegram only when away ------------------------------------------------------


def _tg_config(only_when_away):
    from herdeck.config import TelegramConfig

    config, server = notify_config()
    config.notifications.backends = ["telegram"]
    config.notifications.telegram = TelegramConfig(
        token_env="TG", chat_id="1", only_when_away=only_when_away
    )
    return config


def _tg_sink(config, away):
    from herdeck.notify import NotificationFeed, deckapp_sink

    sent = []
    asked = []

    def is_away(seconds):
        asked.append(seconds)
        return away

    sink = deckapp_sink(
        NotificationFeed(),
        lambda: True,
        config,
        getenv=lambda name: "token",
        telegram_factory=lambda *a: (lambda t, b, s, **kw: sent.append(t)),
        away=is_away,
    )
    return sink, sent, asked


def test_telegram_only_when_away_gates_on_the_away_predicate():
    sink, sent, asked = _tg_sink(_tg_config(10), away=False)
    sink("claude · needs input", "shop", "Glass")
    assert sent == [] and asked == [600.0]

    sink, sent, _asked = _tg_sink(_tg_config(10), away=True)
    sink("claude · needs input", "shop", "Glass")
    assert sent == ["claude · needs input"]


def test_telegram_always_sends_when_the_gate_is_off():
    sink, sent, asked = _tg_sink(_tg_config(0), away=False)
    sink("claude · done", "shop", "Hero")
    assert sent == ["claude · done"] and asked == []


def test_away_needs_idle_input_and_no_recent_deck_press():
    config, server = notify_config()
    src, _notifier, probe = _live(config, server, idle=900.0)
    assert src._user_away(600) is True
    src._last_deck_press = __import__("time").monotonic()  # the deck was just pressed
    assert src._user_away(600) is False
    src._last_deck_press = None
    probe.idle = 30.0
    assert src._user_away(600) is False
    probe.idle = None  # Linux: idle unknown -> only deck presses decide
    assert src._user_away(600) is True


def test_a_deck_press_counts_as_presence():
    from tests.test_deckapp_live import make_live

    app, src, _server, _runner = make_live()
    try:
        assert src._last_deck_press is None
        app.press(0)
        assert src._last_deck_press is not None
    finally:
        app.close()


def test_only_when_away_validates_minutes():
    import pytest

    from herdeck.config import ConfigError, parse_notifications

    tg = {"token_env": "T", "chat_id": "1"}
    assert parse_notifications({"telegram": tg}).telegram.only_when_away == 0
    assert parse_notifications({"telegram": {**tg, "only_when_away": 5}}).telegram.only_when_away == 5
    with pytest.raises(ConfigError, match="notifications.telegram.only_when_away"):
        parse_notifications({"telegram": {**tg, "only_when_away": -5}})
