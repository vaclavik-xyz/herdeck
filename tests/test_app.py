import asyncio

import pytest

from herdeck.app import App, _guard, _run
from herdeck.commands import Command, command_to_msg
from herdeck.config import AnswerProfile, Config, ConfigError, ServerConfig
from herdeck.driver.fake import FakeRenderer
from herdeck.model import AgentKey, AgentState, Status


def make_config():
    return Config(
        servers=[ServerConfig("dev", "wss://x", "t")],
        profiles={
            "claude": AnswerProfile(["1", "enter"], ["esc"], ["ctrl+c"], ["2", "enter"]),
            "default": AnswerProfile(["enter"], ["esc"], ["ctrl+c"], ["enter"]),
        },
        overview_order=["dev"],
        grid=(5, 3),
    )


def blocked(pane="p1"):
    return AgentState(AgentKey("dev", pane), "claude", "api", Status.BLOCKED)


async def test_run_requires_at_least_one_server():
    cfg = Config(
        servers=[],
        profiles={"default": AnswerProfile(["enter"], ["esc"], ["ctrl+c"], ["enter"])},
        overview_order=[],
        grid=(5, 3),
    )
    with pytest.raises(ConfigError, match="no servers configured"):
        await asyncio.wait_for(_run(cfg, FakeRenderer(13)), timeout=0.01)


def test_snapshot_renders_tiles_and_panel():
    deck = FakeRenderer(13)
    app = App(make_config(), deck, send=lambda c: None)
    app.handle_snapshot("dev", [blocked("p1")])
    assert deck.last[0].color == "amber"
    assert deck.last_panel is not None and deck.last_panel.title == "⚠ needs you"


def test_press_forwards_commands():
    deck = FakeRenderer(13)
    sent = []
    app = App(make_config(), deck, send=sent.append)
    app.handle_snapshot("dev", [blocked("p1")])
    deck.simulate_press(0)  # drill + read
    req = app.next_req_for(Command("read", "dev", "p1", source="detection"))
    app.handle_result("dev", req, {"text": "Proceed?\n1. Yes\n2. No", "pane_id": "p1"})
    deck.simulate_press(0)  # choose option 1
    assert Command("read", "dev", "p1", source="detection") in sent
    assert Command("act_if_blocked", "dev", "p1", keys=["1"]) in sent


def test_read_result_shows_detection_in_panel():
    deck = FakeRenderer(13)
    app = App(make_config(), deck, send=lambda c: None)
    app.handle_snapshot("dev", [blocked("p1")])
    deck.simulate_press(0)
    req = app.next_req_for(Command("read", "dev", "p1", source="detection"))
    app.handle_result("dev", req, {"text": "Allow edit?", "pane_id": "p1"})
    assert "Allow edit?" in deck.last_panel.lines[0]


def test_command_to_msg_guard_flags():
    m1 = command_to_msg(Command("act_if_blocked", "dev", "p1", keys=["1"]), "r1")
    assert m1 == {"type": "act", "req": "r1", "pane_id": "p1", "keys": ["1"], "guard": True}
    m2 = command_to_msg(Command("act_force", "dev", "p1", keys=["ctrl+c"]), "r2")
    assert m2["type"] == "act" and m2["guard"] is False


def test_tick_partial_renders_working_tiles():
    deck = FakeRenderer(13)
    app = App(make_config(), deck, send=lambda c: None)
    app.handle_snapshot("dev", [AgentState(AgentKey("dev", "p1"), "claude", "api", Status.WORKING)])
    deck.last = []  # clear to detect a re-render
    app.handle_tick()
    assert deck.last and deck.last[0].spinner == 1


async def test_guard_swallows_exception():
    class Boom:
        async def run(self):
            raise RuntimeError("x")

    await _guard(Boom().run())


async def test_guarded_swallows_cancelled_connector():
    from herdeck.app import _guarded

    class Cancelled:
        async def run(self):
            raise asyncio.CancelledError()

    await _guarded(Cancelled())


# --- read-correlation logic retained from v1 (now asserted via the panel) ---


def test_app_routes_runtime_request_results_before_deck_read_state():
    cfg = make_config()
    app = App(cfg, FakeRenderer(13), send=lambda c: None)

    class Runtime:
        def __init__(self):
            self.results = []

        def handle_result(self, req, data, *, server_id=None):
            self.results.append((server_id, req, data))
            return Command("read", "dev", "p1") if req == "tg1" else None

    runtime = Runtime()
    app.set_runtime_control(runtime)
    app._active_read_req = "r1"
    app.handle_result("dev", "tg1", {"text": "telegram prompt", "pane_id": "p1"})

    assert runtime.results == [("dev", "tg1", {"text": "telegram prompt", "pane_id": "p1"})]
    assert app._active_read_req == "r1"


def test_app_re_lists_after_runtime_action_result():
    cfg = make_config()
    sent = []
    app = App(cfg, FakeRenderer(13), send=lambda c: sent.append(c))

    class Runtime:
        def handle_result(self, req, data, *, server_id=None):
            return Command("send_text", "dev", "p1") if req == "tg1" else None

    app.set_runtime_control(Runtime())
    app.handle_result("dev", "tg1", {"sent": True})

    assert sent == [Command("list", "dev")]


def test_stale_read_result_with_old_req_is_ignored():
    deck = FakeRenderer(13)
    app = App(make_config(), deck, send=lambda c: None)
    app.handle_snapshot("dev", [blocked("p1")])
    deck.simulate_press(0)
    stale = app.next_req_for(Command("read", "dev", "p1", source="detection"))
    app.next_req_for(Command("read", "dev", "p1", source="detection"))  # newer read supersedes
    app.handle_result("dev", stale, {"text": "stale", "pane_id": "p1"})
    assert deck.last_panel.lines == ["reading prompt..."]  # old req ignored


def test_read_result_for_other_pane_is_ignored():
    deck = FakeRenderer(13)
    app = App(make_config(), deck, send=lambda c: None)
    app.handle_snapshot("dev", [blocked("p1")])
    deck.simulate_press(0)  # drilled into p1
    req = app.next_req_for(Command("read", "dev", "p2", source="detection"))
    app.handle_result("dev", req, {"text": "x", "pane_id": "p2"})
    assert deck.last_panel.lines == ["reading prompt..."]  # wrong pane


def test_event_on_drilled_pane_invalidates_inflight_read():
    deck = FakeRenderer(13)
    app = App(make_config(), deck, send=lambda c: None)
    app.handle_snapshot("dev", [blocked("p1")])
    deck.simulate_press(0)
    req = app.next_req_for(Command("read", "dev", "p1", source="detection"))
    app.handle_event("dev", AgentState(AgentKey("dev", "p1"), "claude", "api", Status.WORKING))
    app.handle_result("dev", req, {"text": "stale", "pane_id": "p1"})
    assert deck.last_panel.lines == []  # invalidated by the event


def test_snapshot_changing_drilled_pane_invalidates_read():
    deck = FakeRenderer(13)
    app = App(make_config(), deck, send=lambda c: None)
    app.handle_snapshot("dev", [blocked("p1")])
    deck.simulate_press(0)
    req = app.next_req_for(Command("read", "dev", "p1", source="detection"))
    app.handle_snapshot("dev", [AgentState(AgentKey("dev", "p1"), "claude", "api", Status.WORKING)])
    app.handle_result("dev", req, {"text": "stale", "pane_id": "p1"})
    assert deck.last_panel.lines == []


def test_inflight_read_survives_event_that_keeps_pane_blocked():
    # The event path (remote mode) must also preserve an in-flight read while the
    # drilled agent stays blocked — same guarantee as the snapshot path.
    deck = FakeRenderer(13)
    app = App(make_config(), deck, send=lambda c: None)
    app.handle_snapshot("dev", [blocked("p1")])
    deck.simulate_press(0)  # drill p1, issue read
    req = app.next_req_for(Command("read", "dev", "p1", source="detection"))
    app.handle_event("dev", AgentState(AgentKey("dev", "p1"), "claude", "api·busy", Status.BLOCKED))
    app.handle_result("dev", req, {"text": "Proceed?\n1. Yes\n2. No", "pane_id": "p1"})
    assert deck.last[0].label == "1" and deck.last[0].subtext == "Yes"


def test_detection_survives_event_that_keeps_pane_blocked():
    deck = FakeRenderer(13)
    app = App(make_config(), deck, send=lambda c: None)
    app.handle_snapshot("dev", [blocked("p1")])
    deck.simulate_press(0)
    req = app.next_req_for(Command("read", "dev", "p1", source="detection"))
    app.handle_result("dev", req, {"text": "Proceed?\n1. Yes\n2. No", "pane_id": "p1"})
    assert deck.last[0].label == "1"  # options visible
    app.handle_event("dev", AgentState(AgentKey("dev", "p1"), "claude", "api·busy", Status.BLOCKED))
    assert deck.last[0].label == "1" and deck.last[0].subtext == "Yes"


def test_inflight_read_survives_snapshot_that_keeps_pane_blocked():
    # A routine fleet snapshot (fired by any agent's status change) that re-touches
    # the still-blocked drilled agent must NOT reject the in-flight read — otherwise
    # the prompt never shows and the user has to drill in repeatedly.
    deck = FakeRenderer(13)
    app = App(make_config(), deck, send=lambda c: None)
    app.handle_snapshot("dev", [blocked("p1")])
    deck.simulate_press(0)  # drill p1, issue read
    req = app.next_req_for(Command("read", "dev", "p1", source="detection"))
    # p1 is still blocked; only a cosmetic field changed (e.g. herdr re-derived label)
    app.handle_snapshot(
        "dev", [AgentState(AgentKey("dev", "p1"), "claude", "api·busy", Status.BLOCKED)]
    )
    app.handle_result("dev", req, {"text": "Proceed?\n1. Yes\n2. No", "pane_id": "p1"})
    assert deck.last[0].label == "1" and deck.last[0].subtext == "Yes"


def test_detection_survives_snapshot_that_keeps_pane_blocked():
    # Once shown, the prompt options must not vanish on the next fleet snapshot
    # while the agent is still blocked ("show then disappear" flicker).
    deck = FakeRenderer(13)
    app = App(make_config(), deck, send=lambda c: None)
    app.handle_snapshot("dev", [blocked("p1")])
    deck.simulate_press(0)
    req = app.next_req_for(Command("read", "dev", "p1", source="detection"))
    app.handle_result("dev", req, {"text": "Proceed?\n1. Yes\n2. No", "pane_id": "p1"})
    assert deck.last[0].label == "1"  # options visible
    app.handle_snapshot(
        "dev", [AgentState(AgentKey("dev", "p1"), "claude", "api·busy", Status.BLOCKED)]
    )
    assert deck.last[0].label == "1" and deck.last[0].subtext == "Yes"


def test_tick_uses_partial_render_when_available():
    from herdeck.driver.fake import FakeRenderer

    class PartialFake(FakeRenderer):
        def __init__(self, n):
            super().__init__(n)
            self.partial = None

        def render_working(self, tiles):
            self.partial = tiles

    deck = PartialFake(13)
    app = App(make_config(), deck, send=lambda c: None)
    app.handle_snapshot("dev", [AgentState(AgentKey("dev", "p1"), "claude", "api", Status.WORKING)])
    app.handle_tick()
    assert deck.partial and deck.partial[0].spinner is not None


def test_command_to_msg_focus():
    m = command_to_msg(Command("focus", "dev", "p1"), "r1")
    assert m["type"] == "focus" and m["pane_id"] == "p1" and m["req"]


def test_command_to_msg_start():
    m = command_to_msg(Command("start", "dev", text="claude", keys=["claude"]), "r1")
    assert m["type"] == "start" and m["name"] == "claude" and m["argv"] == ["claude"]


def test_newly_blocked_detects_transition_and_avoids_dup():
    from herdeck.app import newly_blocked
    from herdeck.model import AgentKey, AgentState, Status

    k = AgentKey("s", "p1")
    s_block = [AgentState(k, "claude", "api", Status.BLOCKED)]
    s_work = [AgentState(k, "claude", "api", Status.WORKING)]
    to, seen = newly_blocked(set(), s_block)  # first time -> notify
    assert k in to and k in seen
    to2, seen2 = newly_blocked(seen, s_block)  # same blocked -> no dup
    assert to2 == set() and seen2 == seen
    to3, seen3 = newly_blocked(seen2, s_work)  # left blocked -> reset
    assert to3 == set() and k not in seen3


def test_app_notifies_on_block_transition(monkeypatch):
    from herdeck.notify import Notifier

    calls = []
    cfg = make_config()  # this file's helper
    cfg.notifications.enabled = True
    app = App(
        cfg,
        FakeRenderer(13),
        send=lambda c: None,
        notifier=Notifier(sink=lambda t, b, s: calls.append((t, b))),
    )
    app.handle_snapshot("dev", [AgentState(AgentKey("dev", "p1"), "claude", "api", Status.BLOCKED)])
    assert len(calls) == 1 and "api" in calls[0][1]
    app.handle_snapshot("dev", [AgentState(AgentKey("dev", "p1"), "claude", "api", Status.BLOCKED)])
    assert len(calls) == 1  # no duplicate while still blocked


def test_app_blocked_notifier_receives_agent_state_and_metadata():
    calls = []
    cfg = make_config()
    cfg.notifications.enabled = True

    class CaptureBlocked:
        async def notify_blocked(self, agent, *, body, sound, multi_server):
            calls.append((agent.key, agent.agent_type, body, sound, multi_server))

    app = App(
        cfg,
        FakeRenderer(13),
        send=lambda c: None,
        blocked_notifier=CaptureBlocked(),
    )

    app.handle_snapshot(
        "dev",
        [AgentState(AgentKey("dev", "p1"), "claude", "api", Status.BLOCKED)],
    )

    assert calls == [(AgentKey("dev", "p1"), "claude", "api", True, False)]


async def test_app_default_blocked_notification_scheduler_works_inside_running_loop():
    calls = []
    cfg = make_config()
    cfg.notifications.enabled = True

    class CaptureBlocked:
        async def notify_blocked(self, agent, *, body, sound, multi_server):
            calls.append((agent.key, body, sound, multi_server))

    app = App(
        cfg,
        FakeRenderer(13),
        send=lambda c: None,
        blocked_notifier=CaptureBlocked(),
    )

    app.handle_snapshot(
        "dev",
        [AgentState(AgentKey("dev", "p1"), "claude", "api", Status.BLOCKED)],
    )
    await asyncio.sleep(0)

    assert calls == [(AgentKey("dev", "p1"), "api", True, False)]


async def test_app_direct_blocked_notifier_exception_is_consumed_by_default_scheduler():
    import gc

    cfg = make_config()
    cfg.notifications.enabled = True
    errors = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: errors.append(context))

    class BoomBlocked:
        async def notify_blocked(self, agent, *, body, sound, multi_server):
            raise RuntimeError("blocked notifier failed")

    try:
        app = App(
            cfg,
            FakeRenderer(13),
            send=lambda c: None,
            blocked_notifier=BoomBlocked(),
        )

        app.handle_snapshot(
            "dev",
            [AgentState(AgentKey("dev", "p1"), "claude", "api", Status.BLOCKED)],
        )
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert errors == []


def test_apply_config_rebuilds_blocked_notification_runtime():
    import asyncio

    from herdeck.notify import BlockedNotificationRuntime

    calls = []
    pollers = []

    class CaptureBlocked:
        def __init__(self, label):
            self._label = label

        async def notify_blocked(self, agent, *, body, sound, multi_server):
            calls.append((self._label, agent.key, body, sound, multi_server))

    def runtime_factory(config):
        poller = object()
        pollers.append(poller)
        return BlockedNotificationRuntime(CaptureBlocked(config.meta.active_profile), poller)

    cfg = make_config()
    cfg.meta.active_profile = "default"
    app = App(
        cfg,
        FakeRenderer(13),
        send=lambda c: None,
        blocked_runtime_factory=runtime_factory,
    )
    first_poller = app.notification_poller

    new_cfg = make_config()
    new_cfg.meta.active_profile = "mobile"
    app._apply_config(new_cfg)

    assert app.notification_poller is pollers[-1]
    assert app.notification_poller is not first_poller
    agent = AgentState(AgentKey("dev", "p1"), "claude", "api", Status.BLOCKED)
    asyncio.run(
        app.blocked_notifier.notify_blocked(
            agent, body="api · main", sound=False, multi_server=False
        )
    )
    assert calls == [("mobile", AgentKey("dev", "p1"), "api · main", False, False)]


def test_apply_config_preserves_direct_blocked_notifier():
    calls = []
    cfg = make_config()

    class CaptureBlocked:
        async def notify_blocked(self, agent, *, body, sound, multi_server):
            calls.append((agent.key, body, sound, multi_server))

    app = App(
        cfg,
        FakeRenderer(13),
        send=lambda c: None,
        blocked_notifier=CaptureBlocked(),
    )

    new_cfg = make_config()
    new_cfg.meta.active_profile = "mobile"
    app._apply_config(new_cfg)

    agent = AgentState(AgentKey("dev", "p1"), "claude", "api", Status.BLOCKED)
    asyncio.run(
        app.blocked_notifier.notify_blocked(
            agent, body="api · main", sound=False, multi_server=False
        )
    )

    assert calls == [(AgentKey("dev", "p1"), "api · main", False, False)]


def test_app_notify_keeps_other_servers_blocked_keys():
    from herdeck.notify import Notifier

    calls = []
    cfg = Config(
        servers=[ServerConfig("a", "wss://a", "t"), ServerConfig("b", "wss://b", "t")],
        profiles={"default": AnswerProfile(["enter"], ["esc"], ["ctrl+c"], ["enter"])},
        overview_order=["a", "b"],
        grid=(5, 3),
    )
    cfg.notifications.enabled = True
    app = App(
        cfg,
        FakeRenderer(13),
        send=lambda c: None,
        notifier=Notifier(sink=lambda t, b, s: calls.append((t, b))),
    )
    app.handle_snapshot("a", [AgentState(AgentKey("a", "p1"), "claude", "api", Status.BLOCKED)])
    app.handle_snapshot("b", [AgentState(AgentKey("b", "p1"), "codex", "web", Status.BLOCKED)])
    assert len(calls) == 2
    # Reconciling server "a" must not drop server "b"'s tracked blocked key
    # (else a later re-confirm would notify again).
    app.handle_snapshot("a", [AgentState(AgentKey("a", "p1"), "claude", "api", Status.BLOCKED)])
    app.handle_snapshot("b", [AgentState(AgentKey("b", "p1"), "codex", "web", Status.BLOCKED)])
    assert len(calls) == 2  # no duplicates across servers


def test_app_does_not_notify_when_blocked_not_in_on():
    from herdeck.notify import Notifier

    calls = []
    cfg = make_config()
    cfg.notifications.enabled = True
    cfg.notifications.on = []  # "blocked" not enabled -> no notifications
    app = App(
        cfg,
        FakeRenderer(13),
        send=lambda c: None,
        notifier=Notifier(sink=lambda t, b, s: calls.append((t, b))),
    )
    app.handle_snapshot("dev", [AgentState(AgentKey("dev", "p1"), "claude", "api", Status.BLOCKED)])
    assert calls == []


def test_build_notifier_respects_config():
    from herdeck.app import _build_notifier
    from herdeck.notify import NoopNotifier, Notifier

    cfg = make_config()
    assert isinstance(_build_notifier(cfg), NoopNotifier)  # disabled -> no-op
    cfg.notifications.enabled = True
    n = _build_notifier(cfg)
    assert isinstance(n, Notifier) and not isinstance(n, NoopNotifier)


def test_build_notifier_fires_both_backends():
    from herdeck.app import _build_notifier
    from herdeck.config import TelegramConfig

    calls = []
    cfg = make_config()
    cfg.notifications.enabled = True
    cfg.notifications.backends = ["macos", "telegram"]
    cfg.notifications.telegram = TelegramConfig("HERDECK_TG", "42", message_thread_id=456)

    def rec_macos(t, b, s):
        calls.append(("macos", t))

    def rec_tg(t, b, s):
        calls.append(("telegram", t))

    telegram_args = []
    n = _build_notifier(
        cfg,
        getenv=lambda k: "TOK",
        macos_sink=rec_macos,
        telegram_factory=lambda tok, cid, thread: (
            telegram_args.append((tok, cid, thread)),
            rec_tg,
        )[1],
    )
    n.notify("title", "body", False)
    assert telegram_args == [("TOK", "42", 456)]
    assert ("macos", "title") in calls and ("telegram", "title") in calls


def test_build_blocked_notifier_preserves_macos_with_interactive_telegram():
    from herdeck.app import _build_blocked_notification_runtime
    from herdeck.config import TelegramConfig

    cfg = make_config()
    cfg.notifications.enabled = True
    cfg.notifications.backends = ["macos", "telegram"]
    cfg.notifications.telegram = TelegramConfig(
        "HERDECK_TG",
        "-1001",
        interactive=True,
        allowed_user_ids=[111],
    )
    calls = []

    class Interactive:
        async def notify_blocked(self, agent, *, body, sound, multi_server):
            calls.append(("interactive", agent.key, body, sound, multi_server))

        async def poll_once(self, *, timeout=20, is_current=None):
            calls.append(("poll", timeout))

    def one_way_telegram_should_not_run(token, chat_id, message_thread_id):
        raise AssertionError("interactive Telegram must replace the one-way Telegram sink")

    runtime = _build_blocked_notification_runtime(
        cfg,
        getenv=lambda name: "TOK",
        macos_sink=lambda title, body, sound: calls.append(("macos", title, body, sound)),
        telegram_factory=one_way_telegram_should_not_run,
        telegram_interactor_factory=lambda token, tg: calls.append(
            ("factory", token, tg.chat_id)
        )
        or Interactive(),
    )

    agent = AgentState(AgentKey("local", "p1"), "codex", "herdeck", Status.BLOCKED)
    asyncio.run(
        runtime.notifier.notify_blocked(
            agent, body="herdeck · main", sound=True, multi_server=False
        )
    )
    assert runtime.poller is not None
    asyncio.run(runtime.poller.poll_once(timeout=5))

    assert calls == [
        ("factory", "TOK", "-1001"),
        ("macos", "codex", "herdeck · main", True),
        ("interactive", AgentKey("local", "p1"), "herdeck · main", True, False),
        ("poll", 5),
    ]


def test_build_blocked_runtime_keeps_one_way_telegram_when_interactive_incomplete():
    from herdeck.app import _build_blocked_notification_runtime
    from herdeck.config import TelegramConfig

    cfg = make_config()
    cfg.notifications.enabled = True
    cfg.notifications.backends = ["telegram"]
    cfg.notifications.telegram = TelegramConfig(
        "HERDECK_TG",
        "-1001",
        message_thread_id=456,
        interactive=True,
        allowed_user_ids=[],
    )
    calls = []

    def telegram_sink(token, chat_id, message_thread_id):
        return lambda title, body, sound: calls.append(
            ("telegram", token, chat_id, message_thread_id, title, body, sound)
        )

    def interactor_should_not_run(token, tg):
        raise AssertionError("interactive Telegram requires allowed_user_ids")

    runtime = _build_blocked_notification_runtime(
        cfg,
        getenv=lambda name: "TOK",
        telegram_factory=telegram_sink,
        telegram_interactor_factory=interactor_should_not_run,
    )

    agent = AgentState(AgentKey("local", "p1"), "codex", "herdeck", Status.BLOCKED)
    asyncio.run(
        runtime.notifier.notify_blocked(
            agent, body="herdeck · main", sound=True, multi_server=False
        )
    )

    assert runtime.poller is None
    assert calls == [("telegram", "TOK", "-1001", 456, "codex", "herdeck · main", True)]


def test_build_blocked_runtime_keeps_one_way_telegram_until_interactor_can_poll():
    from herdeck.app import _build_blocked_notification_runtime
    from herdeck.config import TelegramConfig

    cfg = make_config()
    cfg.notifications.enabled = True
    cfg.notifications.backends = ["telegram"]
    cfg.notifications.telegram = TelegramConfig(
        "HERDECK_TG",
        "-1001",
        message_thread_id=456,
        interactive=True,
        allowed_user_ids=[111],
    )
    calls = []

    def telegram_sink(token, chat_id, message_thread_id):
        return lambda title, body, sound: calls.append(
            ("telegram", token, chat_id, message_thread_id, title, body, sound)
        )

    class OutboundOnly:
        async def notify_blocked(self, agent, *, body, sound, multi_server):
            calls.append(("interactive", agent.key, body, sound, multi_server))

    runtime = _build_blocked_notification_runtime(
        cfg,
        getenv=lambda name: "TOK",
        telegram_factory=telegram_sink,
        telegram_interactor_factory=lambda token, tg: OutboundOnly(),
    )

    agent = AgentState(AgentKey("local", "p1"), "codex", "herdeck", Status.BLOCKED)
    asyncio.run(
        runtime.notifier.notify_blocked(
            agent, body="herdeck · main", sound=True, multi_server=False
        )
    )

    assert runtime.poller is None
    assert calls == [("telegram", "TOK", "-1001", 456, "codex", "herdeck · main", True)]


def test_install_telegram_runtime_sets_factory_poller_and_control():
    from herdeck.app import _install_telegram_runtime
    from herdeck.config import TelegramConfig

    cfg = make_config()
    cfg.notifications.enabled = True
    cfg.notifications.backends = ["telegram"]
    cfg.notifications.telegram = TelegramConfig(
        "HERDECK_TG",
        "-1001",
        message_thread_id=456,
        interactive=True,
        allowed_user_ids=[111],
        prompt_max_chars=777,
    )
    updates = []
    made = []

    class Control:
        def update_config(self, config):
            updates.append(config)

    class Poller:
        async def notify_blocked(self, agent, *, body, sound, multi_server):
            pass

        async def poll_once(self, *, timeout=20, is_current=None):
            pass

    control = Control()
    app = App(cfg, FakeRenderer(13), send=lambda c: None)

    _install_telegram_runtime(
        app,
        cfg,
        control,
        getenv=lambda name: "TOK",
        bot_client_factory=lambda token: ("client", token),
        interactor_factory=lambda client, runtime_control, **kwargs: made.append(
            (client, runtime_control, kwargs)
        )
        or Poller(),
    )

    assert updates == [cfg]
    assert made == [
        (
            ("client", "TOK"),
            control,
            {
                "chat_id": "-1001",
                "message_thread_id": 456,
                "allowed_user_ids": [111],
                "prompt_max_chars": 777,
            },
        )
    ]
    assert app.notification_poller is not None


def test_start_telegram_poll_loop_schedules_and_uses_current_poller():
    from herdeck.app import _poll_telegram_once_from_app, _start_telegram_poll_loop
    from herdeck.notify import BlockedNotificationRuntime

    cfg = make_config()
    app = App(cfg, FakeRenderer(13), send=lambda c: None)
    calls = []
    guards = []

    class Poller:
        async def poll_once(self, *, timeout=20, is_current=None):
            guards.append(is_current)
            calls.append(("poll", timeout, is_current()))

    async def fail_sleep(delay):
        raise AssertionError("poller is present; idle sleep should not run")

    app.notification_poller = Poller()
    asyncio.run(_poll_telegram_once_from_app(app, timeout=7, idle_sleep=fail_sleep))

    created = []
    task = _start_telegram_poll_loop(app, create_task=lambda coro: created.append(coro) or object())

    assert calls == [("poll", 7, True)]
    assert guards[0]() is True
    app._install_blocked_runtime(BlockedNotificationRuntime(app.blocked_notifier))
    assert guards[0]() is False
    assert task is not None
    assert created
    created[0].close()


def test_poll_telegram_once_sleeps_after_poller_disables_inbound():
    from herdeck.app import _poll_telegram_once_from_app

    cfg = make_config()
    app = App(cfg, FakeRenderer(13), send=lambda c: None)
    calls = []
    sleeps = []

    class Poller:
        inbound_disabled = False

        async def poll_once(self, *, timeout=20, is_current=None):
            calls.append(("poll", timeout, is_current()))
            self.inbound_disabled = True

    async def record_sleep(delay):
        sleeps.append(delay)

    app.notification_poller = Poller()
    asyncio.run(_poll_telegram_once_from_app(app, timeout=7, idle_sleep=record_sleep))

    assert calls == [("poll", 7, True)]
    assert sleeps == [60]


def test_build_notifier_skips_telegram_without_token():
    from herdeck.app import _build_notifier
    from herdeck.config import TelegramConfig
    from herdeck.notify import NoopNotifier

    cfg = make_config()
    cfg.notifications.enabled = True
    cfg.notifications.backends = ["telegram"]
    cfg.notifications.telegram = TelegramConfig("HERDECK_TG", "42")
    n = _build_notifier(cfg, getenv=lambda k: None)  # token env unset
    assert isinstance(n, NoopNotifier)


def test_build_notifier_skips_telegram_without_config():
    from herdeck.app import _build_notifier
    from herdeck.notify import NoopNotifier

    cfg = make_config()
    cfg.notifications.enabled = True
    cfg.notifications.backends = ["telegram"]  # telegram is None
    n = _build_notifier(cfg, getenv=lambda k: "TOK")
    assert isinstance(n, NoopNotifier)


def test_app_intercepts_switch_profile_without_sending_to_bridge():
    deck = FakeRenderer(13)
    base = make_config()
    base.meta.profile_names = ["work", "mobile"]
    base.meta.active_profile = "work"
    next_cfg = make_config()
    next_cfg.meta.profile_names = ["work", "mobile"]
    next_cfg.meta.active_profile = "mobile"
    sent = []
    persisted = []
    connector_updates = []

    app = App(
        base,
        deck,
        send=sent.append,
        switch_profile=lambda name: (persisted.append(name), next_cfg)[1],
        update_connectors=lambda cfg: connector_updates.append([s.id for s in cfg.servers]),
    )

    app._handle_press(12)
    profiles_index = [t.label for t in deck.last].index("Profiles")
    app._handle_press(profiles_index)
    app._handle_press(1)

    assert sent == []
    assert persisted == ["mobile"]
    assert connector_updates == [["dev"]]
    assert app.config.meta.active_profile == "mobile"
    assert app.orch.config.meta.active_profile == "mobile"


def test_app_ignores_late_snapshot_from_removed_server_after_profile_switch():
    deck = FakeRenderer(13)
    base = make_config()
    base.servers = [
        ServerConfig("old", "wss://old", "t"),
        ServerConfig("dev", "wss://dev", "t"),
    ]
    base.overview_order = ["old", "dev"]
    base.meta.profile_names = ["work", "mobile"]
    base.meta.active_profile = "work"
    next_cfg = make_config()
    next_cfg.servers = [ServerConfig("dev", "wss://dev", "t")]
    next_cfg.overview_order = ["dev"]
    next_cfg.meta.profile_names = ["work", "mobile"]
    next_cfg.meta.active_profile = "mobile"
    app = App(
        base,
        deck,
        send=lambda c: None,
        switch_profile=lambda name: next_cfg,
        update_connectors=lambda cfg: [],
    )
    app.handle_snapshot(
        "old",
        [AgentState(AgentKey("old", "p1"), "claude", "old agent", Status.IDLE)],
    )

    app._handle_switch_profile("mobile")
    app.handle_snapshot(
        "old",
        [AgentState(AgentKey("old", "p2"), "claude", "late old", Status.IDLE)],
    )

    assert "late old" not in [t.label for t in deck.last]


def test_app_clears_agents_for_restarted_server_after_profile_switch():
    deck = FakeRenderer(13)
    base = make_config()
    base.servers = [ServerConfig("dev", "wss://old", "t")]
    base.meta.profile_names = ["work", "mobile"]
    base.meta.active_profile = "work"
    next_cfg = make_config()
    next_cfg.servers = [ServerConfig("dev", "wss://new", "t")]
    next_cfg.meta.profile_names = ["work", "mobile"]
    next_cfg.meta.active_profile = "mobile"
    app = App(
        base,
        deck,
        send=lambda c: None,
        switch_profile=lambda name: next_cfg,
        update_connectors=lambda cfg: {"dev"},
    )
    app.handle_snapshot(
        "dev",
        [AgentState(AgentKey("dev", "p1"), "claude", "old agent", Status.IDLE)],
    )

    app._handle_switch_profile("mobile")

    assert "old agent" not in [t.label for t in deck.last]


def test_app_profile_switch_preserves_online_state_for_unchanged_server():
    deck = FakeRenderer(13)
    base = make_config()
    base.meta.profile_names = ["work", "mobile"]
    base.meta.active_profile = "work"
    next_cfg = make_config()
    next_cfg.meta.profile_names = ["work", "mobile"]
    next_cfg.meta.active_profile = "mobile"
    app = App(
        base,
        deck,
        send=lambda c: None,
        switch_profile=lambda name: next_cfg,
        update_connectors=lambda cfg: set(),
    )
    app.handle_snapshot(
        "dev",
        [AgentState(AgentKey("dev", "p1"), "claude", "api", Status.IDLE)],
    )
    app.handle_connection("dev", True)

    app._handle_switch_profile("mobile")

    assert deck.last[0].label == "api"
    assert deck.last[0].status_text == "IDLE"


def test_app_env_locked_profile_switch_refreshes_tiles_and_shows_panel_message():
    deck = FakeRenderer(13)
    cfg = make_config()
    cfg.meta.profile_names = ["work", "mobile"]
    cfg.meta.active_profile = "work"
    cfg.meta.env_locked_profile = True

    app = App(cfg, deck, send=lambda c: None, switch_profile=lambda name: None)
    app._handle_press(12)
    profiles_index = [t.label for t in deck.last].index("Profiles")
    app._handle_press(profiles_index)
    app._handle_press(1)

    assert deck.last_panel.title == "profile locked"
    assert deck.last[12].label == "+ New"


def test_app_profile_switch_error_keeps_current_config_and_shows_panel():
    deck = FakeRenderer(13)
    cfg = make_config()
    cfg.meta.profile_names = ["work", "mobile"]
    cfg.meta.active_profile = "work"

    def fail_switch(name):
        raise ConfigError("unknown server 'mobile'")

    app = App(cfg, deck, send=lambda c: None, switch_profile=fail_switch)
    app._handle_press(12)
    profiles_index = [t.label for t in deck.last].index("Profiles")
    app._handle_press(profiles_index)
    app._handle_press(1)

    assert app.config.meta.active_profile == "work"
    assert deck.last_panel.title == "profile failed"
    assert "unknown server" in deck.last_panel.lines[0]
    assert deck.last[12].label == "+ New"


def test_connector_manager_diffs_servers():
    from herdeck.app import ConnectorManager
    from herdeck.config import ServerConfig

    made = []
    stopped = []
    tasks = {}

    class FakeConnector:
        def __init__(self, server):
            self.server = server
            made.append(server.id)

        def stop(self):
            stopped.append(self.server.id)

    class FakeTask:
        def __init__(self, server_id):
            self.server_id = server_id
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    def start_connector(conn):
        task = FakeTask(conn.server.id)
        tasks[conn.server.id] = task
        return task

    mgr = ConnectorManager(
        make_connector=lambda server: FakeConnector(server),
        start_connector=start_connector,
    )
    first = mgr.update([ServerConfig("a", "ws://a", "t"), ServerConfig("b", "ws://b", "t")])
    second = mgr.update([ServerConfig("b", "ws://b", "t"), ServerConfig("c", "ws://c", "t")])

    assert made == ["a", "b", "c"]
    assert stopped == ["a"]
    assert first == {"a", "b"}
    assert second == {"c"}
    assert tasks["a"].cancelled is True


def test_make_profile_switcher_resolves_and_persists(tmp_path, monkeypatch):
    from herdeck.app import make_profile_switcher
    from herdeck.settings import load_settings
    from tests.test_settings import OVERLAY_CONFIG

    monkeypatch.setenv("TOK", "secret")
    config = tmp_path / "config.toml"
    config.write_text(
        OVERLAY_CONFIG
        + """
[profiles.work]
servers = ["local"]
"""
    )
    local = tmp_path / "local.toml"
    snapshot = load_settings(config, local)

    switch = make_profile_switcher(snapshot)
    cfg = switch("mobile")

    assert cfg.meta.active_profile == "mobile"
    assert 'active_profile = "mobile"' in local.read_text()


def test_reload_from_disk_applies_new_config():
    deck = FakeRenderer(13)
    app = App(make_config(), deck, send=lambda c: None)
    new_cfg = app.config
    grids = []
    app._apply_config = lambda c: grids.append(c.grid)
    app._config_reloader = lambda: new_cfg
    app.reload_from_disk()
    assert grids == [new_cfg.grid]


def test_reload_from_disk_without_reloader_is_noop():
    deck = FakeRenderer(13)
    app = App(make_config(), deck, send=lambda c: None)
    app._config_reloader = None
    app.reload_from_disk()  # must not raise


def test_reload_from_disk_malformed_file_shows_panel(tmp_path):
    from herdeck.app import make_config_reloader
    from herdeck.settings import load_settings
    from tests.test_settings import OVERLAY_CONFIG

    config = tmp_path / "config.toml"
    config.write_text(OVERLAY_CONFIG)
    local = tmp_path / "local.toml"
    snapshot = load_settings(config, local)

    deck = FakeRenderer(13)
    app = App(make_config(), deck, send=lambda c: None)
    app._config_reloader = make_config_reloader(snapshot)

    old_config = app.config
    config.write_text("this = is not [valid toml")  # mid-edit / truncated write
    app.reload_from_disk()  # must not raise OSError/TOMLDecodeError

    assert app.config is old_config  # current config kept
    assert deck.last_panel is not None and deck.last_panel.title == "reload failed"
