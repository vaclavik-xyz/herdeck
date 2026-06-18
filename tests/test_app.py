from herdeck.app import App
from herdeck.config import Config, ServerConfig, AnswerProfile
from herdeck.driver.fake import FakeRenderer
from herdeck.model import AgentKey, AgentState, Status
from herdeck.orchestrator import Command


def make_config():
    return Config(
        servers=[ServerConfig("workbox", "wss://x", "t")],
        profiles={
            "claude": AnswerProfile(["1", "enter"], ["esc"], ["ctrl+c"],
                                    ["2", "enter"]),
            "default": AnswerProfile(["enter"], ["esc"], ["ctrl+c"], ["enter"]),
        },
        overview_order=["workbox"],
        grid=(5, 3),
    )


def test_snapshot_triggers_render():
    deck = FakeRenderer(15)
    app = App(make_config(), deck, send=lambda cmd: None)
    app.handle_snapshot("workbox", [
        AgentState(AgentKey("workbox", "p1"), "claude", "api", Status.BLOCKED)
    ])
    assert deck.last[0].color == "amber"


def test_press_forwards_commands_to_sender():
    deck = FakeRenderer(15)
    sent = []
    app = App(make_config(), deck, send=sent.append)
    app.handle_snapshot("workbox", [
        AgentState(AgentKey("workbox", "p1"), "claude", "api", Status.BLOCKED)
    ])
    deck.simulate_press(0)             # enter drill-in (read)
    deck.simulate_press(0)             # Approve
    assert Command("read", "workbox", "p1", source="detection") in sent
    assert Command("act_if_blocked", "workbox", "p1",
                   keys=["1", "enter"]) in sent
    # render refreshed after press
    assert deck.last[0].label == "Approve" or deck.last[0].label == "api"


def test_press_is_marshalled_through_schedule():
    deck = FakeRenderer(15)
    sent = []
    pending = []
    app = App(make_config(), deck, send=sent.append,
              schedule=pending.append)   # defer instead of run
    app.handle_snapshot("workbox", [
        AgentState(AgentKey("workbox", "p1"), "claude", "api", Status.BLOCKED)
    ])
    deck.simulate_press(0)
    assert sent == []            # nothing ran yet — it was scheduled
    assert len(pending) == 1
    pending[0]()                 # run the scheduled work
    assert sent == [Command("read", "workbox", "p1", source="detection")]


async def test_guarded_swallows_connector_exception():
    from herdeck.app import _guarded

    class Boom:
        async def run(self):
            raise RuntimeError("boom")

    await _guarded(Boom())   # must not raise


def test_read_result_shows_detection_when_req_and_pane_match():
    deck = FakeRenderer(15)
    app = App(make_config(), deck, send=lambda cmd: None)
    app.handle_snapshot("workbox", [
        AgentState(AgentKey("workbox", "p1"), "claude", "api", Status.BLOCKED)])
    deck.simulate_press(0)                       # enter drill on p1
    req = app.next_req_for(Command("read", "workbox", "p1", source="detection"))
    app.handle_result("workbox", req, {"text": "Allow edit?", "pane_id": "p1"})
    assert deck.last[5].label == "Allow edit?"


def test_stale_read_result_with_old_req_is_ignored():
    deck = FakeRenderer(15)
    app = App(make_config(), deck, send=lambda cmd: None)
    app.handle_snapshot("workbox", [
        AgentState(AgentKey("workbox", "p1"), "claude", "api", Status.BLOCKED)])
    deck.simulate_press(0)
    stale = app.next_req_for(Command("read", "workbox", "p1", source="detection"))
    # a newer read supersedes the active req
    app.next_req_for(Command("read", "workbox", "p1", source="detection"))
    app.handle_result("workbox", stale, {"text": "stale", "pane_id": "p1"})
    assert deck.last[5].label == ""             # old req ignored


def test_read_result_for_other_pane_is_ignored():
    deck = FakeRenderer(15)
    app = App(make_config(), deck, send=lambda cmd: None)
    app.handle_snapshot("workbox", [
        AgentState(AgentKey("workbox", "p1"), "claude", "api", Status.BLOCKED)])
    deck.simulate_press(0)
    req = app.next_req_for(Command("read", "workbox", "p2", source="detection"))
    app.handle_result("workbox", req, {"text": "x", "pane_id": "p2"})
    assert deck.last[5].label == ""             # wrong pane


def test_act_result_triggers_resync_list():
    deck = FakeRenderer(15)
    sent = []
    app = App(make_config(), deck, send=sent.append)
    app.handle_result("workbox", "r9", {"skipped": True})
    assert Command("list", "workbox") in sent
