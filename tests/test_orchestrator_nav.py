from herdeck.config import Config, ServerConfig, AnswerProfile
from herdeck.model import AgentKey, AgentState, Status
from herdeck.orchestrator import (
    Orchestrator, Command, SLOT_NEXT, SLOT_REFRESH,
)


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


def blocked(pane="p1", agent_type="claude"):
    return AgentState(AgentKey("workbox", pane), agent_type, "api", Status.BLOCKED)


def test_press_blocked_agent_enters_drill_and_reads():
    o = Orchestrator(make_config())
    o.apply_snapshot("workbox", [blocked("p1")])
    cmds = o.on_press(0)
    assert cmds == [Command("read", "workbox", "p1", source="detection")]
    labels = [t.label for t in o.render()]
    assert labels[0] == "Approve"
    assert labels[4] == "Back"


def test_press_non_blocked_agent_does_nothing():
    o = Orchestrator(make_config())
    o.apply_snapshot("workbox", [
        AgentState(AgentKey("workbox", "p1"), "claude", "api", Status.WORKING)
    ])
    assert o.on_press(0) == []


def test_approve_resolves_profile_keys():
    o = Orchestrator(make_config())
    o.apply_snapshot("workbox", [blocked("p1")])
    o.on_press(0)                      # enter drill-in
    cmds = o.on_press(0)               # Approve
    assert cmds == [Command("act_if_blocked", "workbox", "p1",
                            keys=["1", "enter"])]


def test_deny_and_stop_keys():
    o = Orchestrator(make_config())
    o.apply_snapshot("workbox", [blocked("p1")])
    o.on_press(0)
    assert o.on_press(2) == [Command("act_if_blocked", "workbox", "p1",
                                     keys=["esc"])]
    o.on_press(0)
    assert o.on_press(3) == [Command("act_if_blocked", "workbox", "p1",
                                     keys=["ctrl+c"])]


def test_back_returns_to_overview():
    o = Orchestrator(make_config())
    o.apply_snapshot("workbox", [blocked("p1")])
    o.on_press(0)
    assert o.on_press(4) == []
    assert o.render()[SLOT_REFRESH].label == "Refresh"  # back in overview


def test_next_jumps_to_first_blocked():
    o = Orchestrator(make_config())
    o.apply_snapshot("workbox", [
        AgentState(AgentKey("workbox", "p0"), "claude", "a", Status.WORKING),
        blocked("p1"),
    ])
    cmds = o.on_press(SLOT_NEXT)
    assert cmds == [Command("read", "workbox", "p1", source="detection")]


def test_refresh_requests_list():
    o = Orchestrator(make_config())
    assert o.on_press(SLOT_REFRESH) == [Command("list", "workbox")]


def test_unknown_agent_type_uses_default_profile():
    o = Orchestrator(make_config())
    o.apply_snapshot("workbox", [blocked("p1", agent_type="mystery")])
    o.on_press(0)
    assert o.on_press(0) == [Command("act_if_blocked", "workbox", "p1",
                                     keys=["enter"])]
