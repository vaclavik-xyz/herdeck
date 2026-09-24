import threading
import time

from herdeck.notify import Notifier, escape_applescript


def test_escape_applescript_quotes_and_backslashes():
    assert escape_applescript('a"b\\c') == 'a\\"b\\\\c'


def test_macos_sink_sound_name_and_switch(monkeypatch):
    import herdeck.notify as notify_mod

    scripts = []
    monkeypatch.setattr(notify_mod.subprocess, "run", lambda cmd, **kw: scripts.append(cmd[2]))
    notify_mod._macos_sink("claude · done", "api", "Hero")
    notify_mod._macos_sink("t", "b", True)
    notify_mod._macos_sink("t", "b", False)
    assert scripts[0] == 'display notification "api" with title "claude · done" sound name "Hero"'
    assert 'sound name "Glass"' in scripts[1]  # True keeps the historical default
    assert "sound name" not in scripts[2]


def test_macos_sink_attaches_the_sound_to_the_notification_only(monkeypatch):
    # One osascript call per alert, sound included: a separately played sound
    # (afplay) would still ring while Focus silences the banner.
    import herdeck.notify as notify_mod

    commands = []
    monkeypatch.setattr(notify_mod.subprocess, "run", lambda cmd, **kw: commands.append(cmd))
    notify_mod._macos_sink("claude · done", "api", "Hero")
    notify_mod._macos_sink("t", "b", "")  # "" = silent
    assert [cmd[0] for cmd in commands] == ["osascript", "osascript"]
    assert 'sound name "Hero"' in commands[0][2]
    assert "sound name" not in commands[1][2]
    assert not hasattr(notify_mod, "play_sound_file")


def test_notification_feed_generation_ack_and_reset_cursor(caplog, monkeypatch):
    from herdeck.notify import NotificationFeed

    caplog.set_level("INFO", logger="herdeck.notify")
    now_ns = 1_000_000_000
    monkeypatch.setattr("herdeck.notify.time.time_ns", lambda: now_ns)

    feed = NotificationFeed()
    first = feed.state()["generation"]

    item = feed.push("codex · done", "api", "Hero")
    assert item["id"] == f"{first}:1"
    now_ns += 37_000_000
    assert feed.ack(first, 1) is True
    assert feed.state()["acked_seq"] == 1
    assert f"notification acknowledged id={first}:1 latency_ms=37" in caplog.text

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
    feed.push("codex · done", "api", "Hero")
    thread.join(timeout=0.3)

    assert not thread.is_alive()
    assert time.monotonic() - started < 0.3
    assert result[0]["items"][0]["title"] == "codex · done"


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
    feed.push("blocked", "p2", "Glass")
    state = feed.wait(item["generation"], 0, timeout=0)
    assert state["items"] == []
    assert feed.ack(item["generation"], 2) is False
    release.set()
    thread.join(timeout=0.2)
    assert not thread.is_alive()
    assert feed.state()["acked_seq"] == 1
    pending = feed.wait(item["generation"], 1, timeout=0)
    assert [queued["seq"] for queued in pending["items"]] == [2]


def test_failed_fallback_does_not_let_newer_item_skip_it():
    from herdeck.notify import NotificationFeed

    feed = NotificationFeed()
    first = feed.push("done", "p1", "Hero")
    delivering = threading.Event()
    release = threading.Event()

    def fail(*_args):
        delivering.set()
        assert release.wait(1)
        raise RuntimeError("osascript failed")

    failures = []

    def run_fallback():
        try:
            feed.fallback(first["generation"], first["seq"], fail)
        except RuntimeError as exc:
            failures.append(str(exc))

    thread = threading.Thread(target=run_fallback)
    thread.start()
    assert delivering.wait(0.2)
    feed.push("blocked", "p2", "Glass")
    assert feed.wait(first["generation"], 0, timeout=0)["items"] == []
    release.set()
    thread.join(timeout=0.2)

    state = feed.wait(first["generation"], 0, timeout=0)
    assert failures == ["osascript failed"]
    assert [item["seq"] for item in state["items"]] == [1, 2]
    assert state["acked_seq"] == 0


def test_notifier_uses_injected_sink():
    calls = []
    n = Notifier(sink=lambda title, body, sound: calls.append((title, body, sound)))
    n.notify("Blocked", "api · main", sound=True)
    assert calls == [("Blocked", "api · main", True)]


def test_notifier_swallows_sink_errors():
    def boom(*a):
        raise RuntimeError("x")

    Notifier(sink=boom).notify("t", "b")  # must not raise


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


def test_feed_default_capacity_holds_a_fleet_burst():
    from herdeck.notify import NotificationFeed

    feed = NotificationFeed()
    for i in range(40):
        feed.push(f"t{i}", "b", False)
    state = feed.wait(None, 0, timeout=0)
    assert len(state["items"]) == 40
    assert feed.dropped == 0


def test_feed_overflow_evicts_acked_history_before_undelivered_items():
    from herdeck.notify import NotificationFeed

    feed = NotificationFeed(maxlen=5)
    for i in range(4):
        feed.push(f"t{i}", "b", False)
    feed_gen = feed.state()["generation"]
    assert feed.ack(feed_gen, 4)  # all four delivered
    for i in range(4, 9):
        feed.push(f"t{i}", "b", False)
    items = feed.state()["items"]
    # Five undelivered items fit exactly: acked history was evicted, none lost.
    assert [item["seq"] for item in items] == [5, 6, 7, 8, 9]
    assert feed.dropped == 0


def test_feed_overflow_of_undelivered_items_is_counted_and_logged(caplog):
    import logging

    from herdeck.notify import NotificationFeed

    feed = NotificationFeed(maxlen=3)
    with caplog.at_level(logging.WARNING, logger="herdeck.notify"):
        for i in range(5):
            feed.push(f"t{i}", "b", False)
    assert [item["seq"] for item in feed.state()["items"]] == [3, 4, 5]
    assert feed.dropped == 2
    assert "overflow" in caplog.text


def test_throttle_cooldown_and_interaction_window():
    from herdeck.notify import NotifyThrottle

    now = [0.0]
    throttle = NotifyThrottle(clock=lambda: now[0], cooldowns={"done": 60.0, "blocked": 5.0})
    assert throttle.allow("done", "a")
    assert not throttle.allow("done", "a")
    assert throttle.allow("blocked", "a")  # separate event
    now[0] = 61
    assert throttle.allow("done", "a")
    throttle.note_interaction("b")
    assert not throttle.allow("done", "b")
    assert throttle.allow("blocked", "b")  # blocked always matters
    now[0] = 80
    assert throttle.allow("done", "b")


def test_event_title_localized():
    from herdeck.notify import event_title

    assert event_title("claude", "blocked") == "claude · needs input"
    assert event_title("codex", "done", "cs") == "codex · hotovo"


def test_banner_icon_rides_the_feed_but_not_the_fallback():
    import herdeck.notify as notify_mod

    feed = notify_mod.NotificationFeed()
    fallback = []
    gate = {"open": True}
    sink = notify_mod.runtime_sink(
        feed, lambda: gate["open"], fallback=lambda t, b, s: fallback.append((t, b, s))
    )
    notifier = Notifier(sink=sink)

    notifier.notify("claude · done", "shop", "Hero", icon="/tmp/notification-icons/v1-p-ab.png")
    notifier.notify("claude · done", "blog", "Hero")
    items = feed.state()["items"]
    assert items[0]["icon"] == "/tmp/notification-icons/v1-p-ab.png"
    assert items[1]["icon"] is None

    # osascript cannot attach images: the fallback keeps its 3-arg contract.
    gate["open"] = False
    notifier.notify("claude · done", "shop", "Hero", icon="/x.png")
    assert fallback == [("claude · done", "shop", "Hero")]


def test_icon_is_only_passed_to_sinks_when_set():
    import herdeck.notify as notify_mod

    plain = []  # a pre-icon three-argument sink keeps working without an icon
    Notifier(sink=lambda t, b, s: plain.append((t, b, s))).notify("t", "b", True)
    assert plain == [("t", "b", True)]

    got = []
    composite = notify_mod.composite_sink(
        [lambda t, b, s: got.append("plain"), lambda t, b, s, icon=None: got.append(icon)]
    )
    composite("t", "b", True)
    composite("t", "b", True, icon="/i.png")
    # No icon -> both run with three args; with one, the icon-aware sink gets it
    # (the plain one raises, which composite_sink isolates and logs).
    assert got == ["plain", None, "/i.png"]


def test_agent_meta_rides_the_feed_only():
    """The shell needs the agent key + episode to open a drill / answer from a
    banner; sinks that know nothing about it (osascript, Telegram) never see it."""
    import herdeck.notify as notify_mod

    feed = notify_mod.NotificationFeed()
    plain = []
    sink = notify_mod.composite_sink(
        [notify_mod.runtime_sink(feed, lambda: True), lambda t, b, s: plain.append((t, b, s))]
    )
    meta = {
        "agent": {"server_id": "prod", "pane_id": "p1"},
        "event": "blocked",
        "episode": "ep1",
        "bogus": "dropped",
        "reply": None,
    }
    Notifier(sink=sink).notify("claude · needs input", "shop", "Glass", meta=meta)

    [item] = feed.state()["items"]
    assert item["kind"] == "alert"
    assert item["agent"] == {"server_id": "prod", "pane_id": "p1"}
    assert item["event"] == "blocked" and item["episode"] == "ep1"
    assert "bogus" not in item and "reply" not in item  # unknown / unset keys stay out
    assert plain == [("claude · needs input", "shop", "Glass")]


def test_meta_is_not_passed_to_a_sink_that_does_not_accept_it():
    got = []
    Notifier(sink=lambda t, b, s, icon=None: got.append(icon)).notify(
        "t", "b", True, icon="/i.png", meta={"event": "done"}
    )
    assert got == ["/i.png"]
