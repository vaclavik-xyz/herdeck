from herdeck.config import Config, ServerConfig, AnswerProfile
from herdeck.driver.base import PanelView, TileView
from herdeck.model import AgentKey, AgentState, Status
from herdeck.orchestrator import Orchestrator


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


def state(pane, status, agent_type="claude", label="api"):
    return AgentState(AgentKey("dev", pane), agent_type, label, status)


def test_overview_orders_blocked_first_and_colors():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [state("p1", Status.IDLE), state("p2", Status.BLOCKED)])
    rs = o.render()
    assert rs.tiles[0].color == "amber"      # blocked first
    assert rs.tiles[0].agent_type == "claude"
    assert rs.tiles[1].color == "blue"       # idle next
    assert isinstance(rs.panel, PanelView)


def test_overview_panel_summary():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [state("p1", Status.BLOCKED), state("p2", Status.WORKING)])
    rs = o.render()
    assert rs.panel.title == "⚠ needs you"
    assert rs.panel.lines[0] == "api"
    assert rs.panel.lines[1].startswith("blocked ")


def test_disconnected_colors_red_and_panel_offline():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [state("p1", Status.IDLE)])
    o.set_connection("dev", False)
    rs = o.render()
    assert rs.tiles[0].color == "red"
    assert rs.panel.title == "OFFLINE"
    assert rs.panel.lines == ["reconnecting…"]


def test_empty_slots_are_dim():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [state("p1", Status.IDLE)])
    rs = o.render()
    assert rs.tiles[1].color == "dim" and rs.tiles[1].label == ""


def test_event_updates_tile():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [state("p1", Status.WORKING)])
    o.apply_event("dev", state("p1", Status.BLOCKED))
    assert o.render().tiles[0].color == "amber"


def test_agent_tile_has_repo_branch_status_and_time():
    clk = [1000.0]
    o = Orchestrator(make_config(), slots=13, clock=lambda: clk[0])
    s = AgentState(AgentKey("dev", "p1"), "claude", "api", Status.WORKING)
    s.repo, s.branch = "macdoktor-crm", "feat/x"
    o.apply_snapshot("dev", [s])
    clk[0] = 1000.0 + 185          # 3 minutes later
    t = o.render().tiles[0]
    assert t.repo == "macdoktor-crm" and t.branch == "feat/x"
    assert t.status_text == "WORKING" and t.time_text == "3m"
