from herdeck.config import Config, ServerConfig, AnswerProfile
from herdeck.model import AgentKey, AgentState, Status
from herdeck.orchestrator import Orchestrator, SLOT_NEXT, SLOT_REFRESH, SLOT_CONN


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


def state(pane, status, agent_type="claude", label="api"):
    return AgentState(AgentKey("workbox", pane), agent_type, label, status)


def test_overview_maps_status_to_color():
    o = Orchestrator(make_config())
    o.apply_snapshot("workbox", [
        state("p1", Status.WORKING),
        state("p2", Status.BLOCKED),
        state("p3", Status.IDLE),
        state("p4", Status.DONE),
    ])
    tiles = {t.index: t for t in o.render()}
    assert tiles[0].color == "green"   # working
    assert tiles[1].color == "amber"   # blocked
    assert tiles[2].color == "blue"    # idle
    assert tiles[3].color == "dim"     # done


def test_overview_has_system_tiles():
    o = Orchestrator(make_config())
    o.set_connection("workbox", True)
    tiles = {t.index: t for t in o.render()}
    assert tiles[SLOT_NEXT].label == "Next"
    assert tiles[SLOT_REFRESH].label == "Refresh"
    assert tiles[SLOT_CONN].color == "green"


def test_connection_tile_red_when_server_down():
    o = Orchestrator(make_config())
    o.set_connection("workbox", False)
    tiles = {t.index: t for t in o.render()}
    assert tiles[SLOT_CONN].color == "red"


def test_event_updates_existing_tile():
    o = Orchestrator(make_config())
    o.apply_snapshot("workbox", [state("p1", Status.WORKING)])
    o.apply_event("workbox", state("p1", Status.BLOCKED))
    tiles = {t.index: t for t in o.render()}
    assert tiles[0].color == "amber"
