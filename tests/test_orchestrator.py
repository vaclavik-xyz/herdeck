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


def test_event_merge_preserves_known_fields():
    o = Orchestrator(make_config())
    o.apply_snapshot("workbox", [
        AgentState(AgentKey("workbox", "p1"), "claude", "api",
                   Status.WORKING, "api")
    ])
    # partial status-only event: agent_type defaulted, label/project empty
    o.apply_event("workbox",
                  AgentState(AgentKey("workbox", "p1"), "default", "",
                             Status.BLOCKED, ""))
    s = o._agents[AgentKey("workbox", "p1")]
    assert s.status is Status.BLOCKED      # updated
    assert s.agent_type == "claude"        # preserved
    assert s.label == "api"               # preserved
    assert s.project == "api"             # preserved


def test_tile_position_stable_across_status_change():
    o = Orchestrator(make_config())
    o.apply_snapshot("workbox", [
        AgentState(AgentKey("workbox", "p1"), "claude", "zzz", Status.WORKING),
        AgentState(AgentKey("workbox", "p2"), "claude", "aaa", Status.WORKING),
    ])
    before = [t.label for t in o.render()][:2]
    # p1's label changes and status flips; tiles must NOT reorder (sorted by pane_id)
    o.apply_event("workbox",
                  AgentState(AgentKey("workbox", "p1"), "claude", "aaa", Status.BLOCKED))
    after_keys = [(o._agents[AgentKey("workbox", "p1")]), (o._agents[AgentKey("workbox", "p2")])]
    tiles = o.render()
    # p1 is still at index 0 (pane_id "p1" < "p2"), regardless of label
    assert tiles[0].color == "amber"   # p1 now blocked, still index 0
    assert tiles[1].color == "green"   # p2 still working at index 1
