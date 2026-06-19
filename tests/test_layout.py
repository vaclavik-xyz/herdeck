from herdeck.driver.base import PanelView
from herdeck.layout import (
    order_agents, page, summary, Counts, status_color,
    panel_overview, panel_detail,
)
from herdeck.model import AgentKey, AgentState, Status


def a(pane, status, agent_type="claude", label="p", server="dev"):
    return AgentState(AgentKey(server, pane), agent_type, label, status)


def test_order_blocked_then_working_then_idle_then_done():
    agents = [a("p1", Status.IDLE), a("p2", Status.BLOCKED),
              a("p3", Status.DONE), a("p4", Status.WORKING)]
    ordered = order_agents(agents, ["dev"])
    assert [s.status for s in ordered] == [
        Status.BLOCKED, Status.WORKING, Status.IDLE, Status.DONE]


def test_order_stable_by_pane_within_status():
    agents = [a("p2", Status.WORKING), a("p1", Status.WORKING)]
    assert [s.key.pane_id for s in order_agents(agents, ["dev"])] == ["p1", "p2"]


def test_page_slices_and_counts():
    items = list(range(30))
    sl, pages = page(items, 0, 13)
    assert sl == list(range(13)) and pages == 3
    sl2, _ = page(items, 1, 13)
    assert sl2 == list(range(13, 26))


def test_page_index_wraps():
    items = list(range(5))
    sl, pages = page(items, 3, 13)   # only 1 page -> wraps to 0
    assert pages == 1 and sl == items


def test_summary_counts():
    agents = [a("p1", Status.BLOCKED), a("p2", Status.WORKING),
              a("p3", Status.WORKING), a("p4", Status.IDLE)]
    c = summary(agents)
    assert (c.blocked, c.working, c.idle, c.done) == (1, 2, 1, 0)


def test_status_color():
    assert status_color(Status.BLOCKED) == "amber"
    assert status_color(Status.WORKING) == "green"
    assert status_color(Status.IDLE) == "blue"
    assert status_color(Status.DONE) == "dim"
    assert status_color(Status.UNKNOWN) == "grey"


def test_panel_overview_online_and_offline():
    c = Counts(1, 4, 6, 0)
    p = panel_overview(c, 0, 2, down=set())
    assert isinstance(p, PanelView) and p.title == "page 1/2"
    assert "B1" in p.lines[0] and "W4" in p.lines[0] and "I6" in p.lines[0]
    assert p.lines[1] == "online" and p.color != "red"
    p2 = panel_overview(c, 0, 2, down={"dev"})
    assert p2.lines[1] == "offline" and p2.color == "red"


def test_panel_detail_with_and_without_text():
    p = panel_detail(a("p1", Status.BLOCKED, agent_type="claude", label="api"),
                     "Allow edit to config.py?")
    assert "claude" in p.title and "api" in p.title
    assert p.lines and "Allow edit" in p.lines[0]
    assert p.color == "amber"
    p2 = panel_detail(a("p1", Status.WORKING), "")
    assert p2.lines == []   # no text yet
