import threading
import time

from herdeck.notify import NoopNotifier, Notifier, escape_applescript


def test_escape_applescript_quotes_and_backslashes():
    assert escape_applescript('a"b\\c') == 'a\\"b\\\\c'


def test_macos_sink_sound_name_and_switch(monkeypatch):
    import herdeck.notify as notify_mod

    scripts = []
    monkeypatch.setattr(notify_mod.subprocess, "run", lambda cmd, **kw: scripts.append(cmd[2]))
    notify_mod._macos_sink("claude done", "api", "Hero")
    notify_mod._macos_sink("t", "b", True)
    notify_mod._macos_sink("t", "b", False)
    assert scripts[0] == 'display notification "api" with title "claude done" sound name "Hero"'
    assert 'sound name "Glass"' in scripts[1]  # True keeps the historical default
    assert "sound name" not in scripts[2]


def test_notification_feed_generation_ack_and_reset_cursor():
    from herdeck.notify import NotificationFeed

    feed = NotificationFeed()
    first = feed.state()["generation"]

    item = feed.push("codex done", "api", "Hero")
    assert item["id"] == f"{first}:1"
    assert feed.ack(first, 1) is True
    assert feed.state()["acked_seq"] == 1

    feed.reset()
    state = feed.state()
    assert state["generation"] != first
    assert state["seq"] == 0
    assert state["acked_seq"] == 0
    assert feed.ack(first, 2) is False


def test_notification_feed_wait_wakes_immediately_for_new_item():
    from herdeck.notify import NotificationFeed

    feed = NotificationFeed()
    generation = feed.state()["generation"]
    result = []
    ready = threading.Event()

    def waiter():
        ready.set()
        result.append(feed.wait(generation, 0, timeout=1.0))

    thread = threading.Thread(target=waiter)
    thread.start()
    assert ready.wait(0.2)
    started = time.monotonic()
    feed.push("codex done", "api", "Hero")
    thread.join(timeout=0.3)

    assert not thread.is_alive()
    assert time.monotonic() - started < 0.3
    assert result[0]["items"][0]["title"] == "codex done"


def test_notification_feed_long_poll_cursor_repairs_lost_ack():
    from herdeck.notify import NotificationFeed

    feed = NotificationFeed()
    item = feed.push("done", "p1", "Hero")
    state = feed.wait(item["generation"], 1, timeout=0)
    assert state["acked_seq"] == 1
    assert state["items"] == []


def test_notification_feed_hides_item_while_fallback_is_in_flight():
    from herdeck.notify import NotificationFeed

    feed = NotificationFeed()
    item = feed.push("done", "p1", "Hero")
    delivering = threading.Event()
    release = threading.Event()

    def deliver(*_args):
        delivering.set()
        assert release.wait(1)

    thread = threading.Thread(
        target=lambda: feed.fallback(item["generation"], item["seq"], deliver)
    )
    thread.start()
    assert delivering.wait(0.2)
    state = feed.wait(item["generation"], 0, timeout=0)
    assert state["items"] == []
    release.set()
    thread.join(timeout=0.2)
    assert not thread.is_alive()
    assert feed.state()["acked_seq"] == 1


def test_noop_notifier_never_raises():
    NoopNotifier().notify("t", "b", sound=True)  # no exception, no side effect


def test_notifier_uses_injected_sink():
    calls = []
    n = Notifier(sink=lambda title, body, sound: calls.append((title, body, sound)))
    n.notify("Blocked", "api · main", sound=True)
    assert calls == [("Blocked", "api · main", True)]


def test_notifier_swallows_sink_errors():
    def boom(*a):
        raise RuntimeError("x")

    Notifier(sink=boom).notify("t", "b")  # must not raise


def test_legacy_blocked_notifier_uses_agent_type_title_and_body():
    import asyncio

    from herdeck.model import AgentKey, AgentState, Status
    from herdeck.notify import LegacyBlockedNotifier, Notifier

    calls = []
    notifier = LegacyBlockedNotifier(
        Notifier(sink=lambda title, body, sound: calls.append((title, body, sound)))
    )
    agent = AgentState(AgentKey("local", "p1"), "codex", "herdeck", Status.BLOCKED)

    asyncio.run(notifier.notify_blocked(agent, body="herdeck · main", sound=True, multi_server=False))

    assert calls == [("codex", "herdeck · main", True)]


def test_composite_blocked_notifier_calls_all_even_if_one_raises():
    import asyncio

    from herdeck.model import AgentKey, AgentState, Status
    from herdeck.notify import CompositeBlockedNotifier

    agent = AgentState(AgentKey("local", "p1"), "codex", "herdeck", Status.BLOCKED)
    calls = []

    class Boom:
        async def notify_blocked(self, agent, *, body, sound, multi_server):
            raise RuntimeError("x")

    class Rec:
        async def notify_blocked(self, agent, *, body, sound, multi_server):
            calls.append((agent.key.pane_id, body, sound, multi_server))

    asyncio.run(
        CompositeBlockedNotifier([Rec(), Boom(), Rec()]).notify_blocked(
            agent, body="body", sound=False, multi_server=True
        )
    )

    assert calls == [("p1", "body", False, True), ("p1", "body", False, True)]


def test_telegram_sink_builds_url_and_payload():
    from herdeck.notify import make_telegram_sink

    sent = []
    sink = make_telegram_sink("TOK", "42", post=lambda url, fields: sent.append((url, fields)))
    sink("Blocked", "api · main", True)
    url, fields = sent[0]
    assert url == "https://api.telegram.org/botTOK/sendMessage"
    assert fields["chat_id"] == "42"
    assert fields["text"] == "Blocked\napi · main"
    assert fields["disable_notification"] == "false"  # sound=True -> not silent


def test_telegram_sink_silent_when_no_sound():
    from herdeck.notify import make_telegram_sink

    sent = []
    sink = make_telegram_sink("TOK", "42", post=lambda url, fields: sent.append(fields))
    sink("t", "b", False)
    assert sent[0]["disable_notification"] == "true"


def test_telegram_sink_includes_topic_when_configured():
    from herdeck.notify import make_telegram_sink

    sent = []
    sink = make_telegram_sink(
        "TOK",
        "-1001",
        message_thread_id=456,
        post=lambda url, fields: sent.append(fields),
    )

    sink("Blocked", "api · main", True)

    assert sent[0]["message_thread_id"] == "456"


def test_deckapp_sink_honors_backends_and_disabled():
    import types

    from herdeck import notify as notify_mod

    feed = notify_mod.NotificationFeed()

    def make_config(backends, enabled=True, token="tok", chat="chat"):
        tg = types.SimpleNamespace(token_env="T", chat_id=chat, message_thread_id=None)
        return types.SimpleNamespace(
            notifications=types.SimpleNamespace(
                enabled=enabled, backends=list(backends), telegram=tg
            )
        )

    def make_feed():
        return notify_mod.NotificationFeed()

    # notifications disabled: nothing fires at all.
    calls = []
    feed = make_feed()
    sink = notify_mod.deckapp_sink(
        feed,
        lambda: True,
        make_config(["macos"], enabled=False),
        sound_player=lambda name: calls.append(("sound", name)) or True,
        getenv=lambda name: "tok",
        telegram_factory=lambda *a, **kw: (lambda t, b, s: calls.append(("tg", t))),
    )
    sink("t", "b", "Glass")
    assert calls == [] and feed.state()["items"] == []

    # macos only: shell-owned feed, no runtime sound and no telegram.
    calls = []
    feed = make_feed()
    sink = notify_mod.deckapp_sink(
        feed,
        lambda: True,
        make_config(["macos"]),
        sound_player=lambda name: calls.append(("sound", name)) or True,
        getenv=lambda name: "tok",
        telegram_factory=lambda *a, **kw: (lambda t, b, s: calls.append(("tg", t))),
    )
    sink("t", "b", "Glass")
    assert calls == [] and feed.state()["items"]

    # telegram only: telegram fires, no sound, no feed recording.
    calls = []
    feed = make_feed()
    sink = notify_mod.deckapp_sink(
        feed,
        lambda: True,
        make_config(["telegram"]),
        sound_player=lambda name: calls.append(("sound", name)) or True,
        getenv=lambda name: "tok",
        telegram_factory=lambda *a, **kw: (lambda t, b, s: calls.append(("tg", t))),
    )
    sink("t", "b", "Glass")
    assert calls == [("tg", "t")] and feed.state()["items"] == []

    # both: shell-owned feed + telegram (the shell plays sound after showing).
    calls = []
    feed = make_feed()
    sink = notify_mod.deckapp_sink(
        feed,
        lambda: True,
        make_config(["macos", "telegram"]),
        sound_player=lambda name: calls.append(("sound", name)) or True,
        getenv=lambda name: "tok",
        telegram_factory=lambda *a, **kw: (lambda t, b, s: calls.append(("tg", t))),
    )
    sink("t", "b", "Glass")
    assert calls == [("tg", "t")]


def test_composite_sink_calls_all_even_if_one_raises():
    from herdeck.notify import composite_sink

    calls = []

    def boom(*a):
        raise RuntimeError("x")

    sink = composite_sink(
        [
            lambda t, b, s: calls.append(("a", t)),
            boom,
            lambda t, b, s: calls.append(("c", t)),
        ]
    )
    sink("title", "body", True)
    assert calls == [("a", "title"), ("c", "title")]


def test_delivery_failure_is_visible_and_rate_limited(monkeypatch, caplog):
    """A wrong bot token must not vanish at DEBUG (audit: notify-failures-visible)."""
    import logging

    from herdeck import notify as notify_mod

    t = [1000.0]
    monkeypatch.setattr(notify_mod, "_monotonic", lambda: t[0])
    monkeypatch.setattr(notify_mod, "_last_warned", {})

    def failing(title, body, sound):
        raise RuntimeError("Unauthorized")

    failing._notify_name = "telegram"
    n = Notifier(sink=failing)
    with caplog.at_level(logging.WARNING, logger="herdeck.notify"):
        n.notify("t", "b")
        n.notify("t", "b")  # identical failure inside the window -> DEBUG only
        t[0] += 600
        n.notify("t", "b")  # window elapsed -> visible again
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "telegram" in warnings[0].getMessage()
    assert "Unauthorized" in warnings[0].getMessage()
