from herdeck.app import App, _command_to_msg, _guard
from herdeck.config import Config, ServerConfig, AnswerProfile
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


def test_snapshot_renders_tiles_and_panel():
    deck = FakeRenderer(13)
    app = App(make_config(), deck, send=lambda c: None)
    app.handle_snapshot("dev", [blocked("p1")])
    assert deck.last[0].color == "amber"
    assert deck.last_panel is not None and deck.last_panel.title.startswith("page 1/")


def test_press_forwards_commands():
    deck = FakeRenderer(13)
    sent = []
    app = App(make_config(), deck, send=sent.append)
    app.handle_snapshot("dev", [blocked("p1")])
    deck.simulate_press(0)        # drill + read
    deck.simulate_press(0)        # Approve
    assert Command("read", "dev", "p1", source="detection") in sent
    assert Command("act_if_blocked", "dev", "p1", keys=["1", "enter"]) in sent


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
