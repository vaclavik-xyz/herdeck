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


def test_panel_overview_offline_takes_priority():
    pv = panel_overview(Counts(1, 0, 0, 0), 0, 1, {"srv"}, 5, ("api", "2m"))
    assert isinstance(pv, PanelView)
    assert pv.title == "OFFLINE"
    assert pv.color == "red"


def test_panel_overview_blocked_spotlight():
    pv = panel_overview(Counts(1, 3, 6, 0), 0, 1, set(), 11, ("macdoktor-crm", "4m"))
    assert pv.title == "⚠ needs you"
    assert pv.lines[0] == "macdoktor-crm"
    assert pv.lines[1] == "blocked 4m"
    assert pv.color == "amber"


def test_panel_overview_blocked_without_elapsed():
    pv = panel_overview(Counts(1, 0, 0, 0), 0, 1, set(), 1, ("api", ""))
    assert pv.lines[1] == "blocked"


def test_panel_overview_calm():
    pv = panel_overview(Counts(0, 3, 6, 2), 0, 1, set(), 11, None)
    assert pv.title == "11 agents"
    assert pv.lines[0] == "W3 · I6 · D2"
    assert pv.lines[1] == "online"
    assert pv.color == "grey"


def test_panel_overview_page_suffix_only_when_multipage():
    multi = panel_overview(Counts(0, 1, 0, 0), 1, 3, set(), 5, None)
    assert multi.lines[-1].endswith(" · 2/3")
    single = panel_overview(Counts(0, 1, 0, 0), 0, 1, set(), 5, None)
    assert "/" not in single.lines[-1]


def test_panel_detail_with_and_without_text():
    p = panel_detail(a("p1", Status.BLOCKED, agent_type="claude", label="api"),
                     "Allow edit to config.py?")
    assert "claude" in p.title and "api" in p.title
    assert p.lines and "Allow edit" in p.lines[0]
    assert p.color == "amber"
    p2 = panel_detail(a("p1", Status.WORKING), "")
    assert p2.lines == []   # no text yet


def test_panel_detail_shows_question_not_option_lines():
    agent = AgentState(AgentKey("s", "p"), "claude", "api", Status.BLOCKED)
    panel = panel_detail(agent, "Do you want to proceed?\n1. Yes\n2. No")
    assert panel.lines == ["Do you want to proceed?"]


def test_panel_detail_all_options_falls_back_to_first_line():
    agent = AgentState(AgentKey("s", "p"), "claude", "api", Status.BLOCKED)
    panel = panel_detail(agent, "1. Yes\n2. No")
    assert panel.lines and "1." in panel.lines[0]


def test_parse_options_numbered():
    from herdeck.layout import parse_options
    txt = "Do you want to proceed?\n❯ 1. Yes\n  2. Yes, and don't ask again\n  3. No"
    opts = parse_options(txt)
    assert [(o.key, o.label) for o in opts] == [
        ("1", "Yes"), ("2", "Yes, and don't ask again"), ("3", "No")]


def test_parse_options_question_list_and_dedup():
    from herdeck.layout import parse_options
    txt = ("Kde?\n1. Dodavatelské doklady\n   detail line\n"
           "2. Cenotvorba / sledování trhu\n2. duplicate ignored\n5. Type something")
    opts = parse_options(txt)
    assert [o.key for o in opts] == ["1", "2", "5"]
    assert opts[0].label == "Dodavatelské doklady"


def test_parse_options_none():
    from herdeck.layout import parse_options
    assert parse_options("just some text, no options") == []
    assert parse_options("") == []
