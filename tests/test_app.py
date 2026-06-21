import asyncio

import pytest

from herdeck.app import App, _command_to_msg, _guard, _run
from herdeck.config import AnswerProfile, Config, ConfigError, ServerConfig
from herdeck.driver.fake import FakeRenderer
from herdeck.model import AgentKey, AgentState, Status
from herdeck.orchestrator import Command


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
    deck.simulate_press(0)        # drill + read
    req = app.next_req_for(Command("read", "dev", "p1", source="detection"))
    app.handle_result("dev", req,
                      {"text": "Proceed?\n1. Yes\n2. No", "pane_id": "p1"})
    deck.simulate_press(0)        # choose option 1
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
    app = App(make_config(), FakeRenderer(13), send=lambda c: None)
    m1 = _command_to_msg(Command("act_if_blocked", "dev", "p1", keys=["1"]), app)
    assert m1 == {"type": "act", "req": m1["req"], "pane_id": "p1",
                  "keys": ["1"], "guard": True}
    m2 = _command_to_msg(Command("act_force", "dev", "p1", keys=["ctrl+c"]), app)
    assert m2["type"] == "act" and m2["guard"] is False


def test_tick_partial_renders_working_tiles():
    deck = FakeRenderer(13)
    app = App(make_config(), deck,
              send=lambda c: None)
    app.handle_snapshot("dev", [AgentState(AgentKey("dev", "p1"), "claude", "api",
                                           Status.WORKING)])
    deck.last = []                # clear to detect a re-render
    app.handle_tick()
    assert deck.last and deck.last[0].spinner == 1


async def test_guard_swallows_exception():
    class Boom:
        async def run(self): raise RuntimeError("x")
    await _guard(Boom().run())


# --- read-correlation logic retained from v1 (now asserted via the panel) ---

def test_stale_read_result_with_old_req_is_ignored():
    deck = FakeRenderer(13)
    app = App(make_config(), deck, send=lambda c: None)
    app.handle_snapshot("dev", [blocked("p1")])
    deck.simulate_press(0)
    stale = app.next_req_for(Command("read", "dev", "p1", source="detection"))
    app.next_req_for(Command("read", "dev", "p1", source="detection"))  # newer read supersedes
    app.handle_result("dev", stale, {"text": "stale", "pane_id": "p1"})
    assert deck.last_panel.lines == []          # old req ignored


def test_read_result_for_other_pane_is_ignored():
    deck = FakeRenderer(13)
    app = App(make_config(), deck, send=lambda c: None)
    app.handle_snapshot("dev", [blocked("p1")])
    deck.simulate_press(0)                       # drilled into p1
    req = app.next_req_for(Command("read", "dev", "p2", source="detection"))
    app.handle_result("dev", req, {"text": "x", "pane_id": "p2"})
    assert deck.last_panel.lines == []           # wrong pane


def test_event_on_drilled_pane_invalidates_inflight_read():
    deck = FakeRenderer(13)
    app = App(make_config(), deck, send=lambda c: None)
    app.handle_snapshot("dev", [blocked("p1")])
    deck.simulate_press(0)
    req = app.next_req_for(Command("read", "dev", "p1", source="detection"))
    app.handle_event("dev", AgentState(AgentKey("dev", "p1"), "claude", "api",
                                       Status.WORKING))
    app.handle_result("dev", req, {"text": "stale", "pane_id": "p1"})
    assert deck.last_panel.lines == []           # invalidated by the event


def test_snapshot_changing_drilled_pane_invalidates_read():
    deck = FakeRenderer(13)
    app = App(make_config(), deck, send=lambda c: None)
    app.handle_snapshot("dev", [blocked("p1")])
    deck.simulate_press(0)
    req = app.next_req_for(Command("read", "dev", "p1", source="detection"))
    app.handle_snapshot("dev", [AgentState(AgentKey("dev", "p1"), "claude", "api",
                                           Status.WORKING)])
    app.handle_result("dev", req, {"text": "stale", "pane_id": "p1"})
    assert deck.last_panel.lines == []


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
    app.handle_snapshot("dev", [AgentState(AgentKey("dev", "p1"), "claude", "api",
                                           Status.WORKING)])
    app.handle_tick()
    assert deck.partial and deck.partial[0].spinner is not None


def test_command_to_msg_focus():
    app = App(make_config(), FakeRenderer(13), send=lambda c: None)
    m = _command_to_msg(Command("focus", "dev", "p1"), app)
    assert m["type"] == "focus" and m["pane_id"] == "p1" and m["req"]


def test_command_to_msg_start():
    app = App(make_config(), FakeRenderer(13), send=lambda c: None)
    m = _command_to_msg(Command("start", "dev", text="claude", keys=["claude"]), app)
    assert m["type"] == "start" and m["name"] == "claude" and m["argv"] == ["claude"]


def test_newly_blocked_detects_transition_and_avoids_dup():
    from herdeck.app import newly_blocked
    from herdeck.model import AgentKey, AgentState, Status
    k = AgentKey("s", "p1")
    s_block = [AgentState(k, "claude", "api", Status.BLOCKED)]
    s_work = [AgentState(k, "claude", "api", Status.WORKING)]
    to, seen = newly_blocked(set(), s_block)          # first time -> notify
    assert k in to and k in seen
    to2, seen2 = newly_blocked(seen, s_block)         # same blocked -> no dup
    assert to2 == set() and seen2 == seen
    to3, seen3 = newly_blocked(seen2, s_work)         # left blocked -> reset
    assert to3 == set() and k not in seen3


def test_app_notifies_on_block_transition(monkeypatch):
    from herdeck.notify import Notifier
    calls = []
    cfg = make_config()                 # this file's helper
    cfg.notifications.enabled = True
    app = App(cfg, FakeRenderer(13), send=lambda c: None,
              notifier=Notifier(sink=lambda t, b, s: calls.append((t, b))))
    app.handle_snapshot("dev", [AgentState(AgentKey("dev", "p1"), "claude", "api",
                                           Status.BLOCKED)])
    assert len(calls) == 1 and "api" in calls[0][1]
    app.handle_snapshot("dev", [AgentState(AgentKey("dev", "p1"), "claude", "api",
                                           Status.BLOCKED)])
    assert len(calls) == 1            # no duplicate while still blocked


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
    app = App(cfg, FakeRenderer(13), send=lambda c: None,
              notifier=Notifier(sink=lambda t, b, s: calls.append((t, b))))
    app.handle_snapshot("a", [AgentState(AgentKey("a", "p1"), "claude", "api",
                                         Status.BLOCKED)])
    app.handle_snapshot("b", [AgentState(AgentKey("b", "p1"), "codex", "web",
                                         Status.BLOCKED)])
    assert len(calls) == 2
    # Reconciling server "a" must not drop server "b"'s tracked blocked key
    # (else a later re-confirm would notify again).
    app.handle_snapshot("a", [AgentState(AgentKey("a", "p1"), "claude", "api",
                                         Status.BLOCKED)])
    app.handle_snapshot("b", [AgentState(AgentKey("b", "p1"), "codex", "web",
                                         Status.BLOCKED)])
    assert len(calls) == 2            # no duplicates across servers


def test_app_does_not_notify_when_blocked_not_in_on():
    from herdeck.notify import Notifier
    calls = []
    cfg = make_config()
    cfg.notifications.enabled = True
    cfg.notifications.on = []          # "blocked" not enabled -> no notifications
    app = App(cfg, FakeRenderer(13), send=lambda c: None,
              notifier=Notifier(sink=lambda t, b, s: calls.append((t, b))))
    app.handle_snapshot("dev", [AgentState(AgentKey("dev", "p1"), "claude", "api",
                                           Status.BLOCKED)])
    assert calls == []
