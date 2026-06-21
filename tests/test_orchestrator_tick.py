from herdeck.config import AnswerProfile, Config, ServerConfig
from herdeck.model import AgentKey, AgentState, Status
from herdeck.orchestrator import Orchestrator


def make_config():
    return Config(
        servers=[ServerConfig("dev", "wss://x", "t")],
        profiles={"default": AnswerProfile(["enter"], ["esc"], ["ctrl+c"], ["enter"])},
        overview_order=["dev"],
        grid=(5, 3),
    )


def st(pane, status):
    return AgentState(AgentKey("dev", pane), "claude", "api", status)


def test_tick_advances_phase_and_reports_working_tiles():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.WORKING), st("p2", Status.IDLE)])
    working = o.tick()
    assert working == [0]                       # only the working tile index
    assert o.render().tiles[0].spinner == 1     # phase advanced and applied
    o.tick()
    assert o.render().tiles[0].spinner == 2


def test_tick_no_working_returns_empty():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.IDLE)])
    assert o.tick() == []


def test_tick_noop_in_drill():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.WORKING)])
    o.on_press(0)              # enter drill
    assert o.tick() == []      # no spinner work while drilled in
