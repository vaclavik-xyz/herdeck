from herdeck.config import Config, ServerConfig, AnswerProfile
from herdeck.model import AgentKey, AgentState, Status
from herdeck.orchestrator import Orchestrator, Command


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


def st(pane, status, agent_type="claude", label="api"):
    return AgentState(AgentKey("dev", pane), agent_type, label, status)


def test_tap_agent_enters_drill_and_reads():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED)])
    cmds = o.on_press(0)
    assert cmds == [Command("read", "dev", "p1", source="detection")]
    rs = o.render()
    assert [t.label for t in rs.tiles[:5]] == ["Approve", "Approve!", "Deny", "Stop", "Back"]


def test_drill_panel_shows_detail_after_read_result():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED)])
    o.on_press(0)
    o.set_detection("Allow edit?")
    assert "Allow edit?" in o.render().panel.lines[0]


def test_approve_enabled_only_when_blocked():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.WORKING)])
    o.on_press(0)                       # drill into a working agent
    assert o.on_press(0) == []          # Approve disabled (not blocked)
    assert o.on_press(2) == []          # Deny disabled


def test_approve_keys_when_blocked():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED)])
    o.on_press(0)
    assert o.on_press(0) == [Command("act_if_blocked", "dev", "p1", keys=["1", "enter"])]


def test_stop_works_even_when_not_blocked():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.WORKING)])
    o.on_press(0)
    assert o.on_press(3) == [Command("act_force", "dev", "p1", keys=["ctrl+c"])]


def test_back_returns_to_overview():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED)])
    o.on_press(0)
    assert o.on_press(4) == []
    assert o.render().tiles[0].agent_type == "claude"  # overview again


def test_panel_press_pages_in_overview():
    o = Orchestrator(make_config(), slots=2)   # tiny deck -> paging
    o.apply_snapshot("dev", [st("p1", Status.IDLE), st("p2", Status.IDLE),
                             st("p3", Status.IDLE)])
    first = [t.label for t in o.render().tiles]
    assert o.on_press(13) == []        # panel press -> next page, no command
    second = [t.label for t in o.render().tiles]
    assert first != second
    assert o.render().panel.title == "page 2/2"


def test_unknown_agent_type_uses_default_profile():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED, agent_type="mystery")])
    o.on_press(0)
    assert o.on_press(0) == [Command("act_if_blocked", "dev", "p1", keys=["enter"])]
