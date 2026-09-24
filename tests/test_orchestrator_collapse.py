"""[view].collapse_idle: idle agents fold into one "+N idle" tile that expands
on press (and folds back from a "hide" tile)."""

from herdeck.config import AnswerProfile, Config, ServerConfig
from herdeck.model import AgentKey, AgentState, Status
from herdeck.orchestrator import MENU_IDLE_TIMEOUT_S, Orchestrator

PANEL = 13


def make_config(collapse=True, lang="en"):
    cfg = Config(
        servers=[ServerConfig("dev", "wss://x", "t")],
        profiles={"default": AnswerProfile(["enter"], ["esc"], ["ctrl+c"], ["enter"])},
        overview_order=["dev"],
        grid=(5, 3),
    )
    cfg.view.collapse_idle = collapse
    cfg.view.language = lang
    return cfg


def st(pane, status):
    return AgentState(AgentKey("dev", pane), "claude", pane, status)


def make(agents, collapse=True, lang="en"):
    t = {"now": 0.0}
    o = Orchestrator(make_config(collapse, lang), slots=13, clock=lambda: t["now"])
    o.apply_snapshot("dev", agents)
    t["now"] = 10.0  # beyond the slot-press guard
    return o, t


def labels(o):
    return [tile.label for tile in o.render().tiles]


def test_idle_agents_fold_into_one_tile_at_the_end():
    o, _ = make([st("w1", Status.WORKING), st("i1", Status.IDLE), st("i2", Status.IDLE), st("b1", Status.BLOCKED)])
    tiles = o.render().tiles
    assert [t.label for t in tiles[:3]] == ["b1", "w1", "+2"]
    group = tiles[2]
    assert group.color == "grey" and group.subtext == "idle · show" and group.agent_type is None
    assert tiles[3].label == ""


def test_collapse_is_off_by_default():
    o, _ = make([st("w1", Status.WORKING), st("i1", Status.IDLE)], collapse=False)
    assert labels(o)[:2] == ["w1", "i1"]


def test_no_group_tile_without_idle_agents():
    o, _ = make([st("w1", Status.WORKING)])
    assert labels(o)[:2] == ["w1", ""]


def test_group_label_is_localized():
    o, _ = make([st("i1", Status.IDLE), st("i2", Status.IDLE)], lang="cs")
    tile = o.render().tiles[0]
    assert tile.label == "+2" and tile.subtext == "nečinní · zobrazit"
    o.on_press(0)
    hide = o.render().tiles[2]
    assert hide.label == "skrýt" and hide.subtext == "nečinné"


def test_pressing_the_group_expands_and_the_hide_tile_folds_back():
    o, _ = make([st("w1", Status.WORKING), st("i1", Status.IDLE), st("i2", Status.IDLE)])
    assert o.on_press(1) == []  # "+2"
    assert labels(o)[:4] == ["w1", "i1", "i2", "hide"]
    assert o.on_press(3) == []  # "hide"
    assert labels(o)[:2] == ["w1", "+2"]


def test_collapsed_idle_frees_pages():
    agents = [st(f"w{i:02}", Status.WORKING) for i in range(10)] + [st(f"i{i:02}", Status.IDLE) for i in range(10)]
    o, _ = make(agents)
    panel = o.render().panel
    assert not any("/" in line for line in panel.lines)  # 11 tiles fit on one page
    assert labels(o)[10] == "+10"


def test_expanding_jumps_to_the_page_with_the_first_idle_agent():
    agents = [st(f"w{i:02}", Status.WORKING) for i in range(11)] + [st(f"i{i:02}", Status.IDLE) for i in range(5)]
    o, _ = make(agents)
    # 11 working + group = 12 entries over 12 agent slots... the group is last
    assert labels(o)[11] == "+5"
    o.on_press(11)
    assert o._page == 0  # the first idle agent (index 11) is still on page 0
    assert labels(o)[11] == "i00"
    o.on_press(PANEL)  # page 2 holds the rest and the hide tile
    assert labels(o)[:5] == ["i01", "i02", "i03", "i04", "hide"]
    o.on_press(4)
    assert o._page == 0 and labels(o)[11] == "+5"


def test_expand_moves_to_a_later_page_when_needed():
    agents = [st(f"w{i:02}", Status.WORKING) for i in range(13)] + [st(f"i{i:02}", Status.IDLE) for i in range(3)]
    o, _ = make(agents)
    o.on_press(PANEL)  # page 2: w12 + "+3"
    assert labels(o)[:2] == ["w12", "+3"]
    o.on_press(1)
    assert labels(o)[:5] == ["w12", "i00", "i01", "i02", "hide"]


def test_pinned_idle_agent_stays_visible_when_collapsed():
    o, _ = make([st("w1", Status.WORKING), st("i1", Status.IDLE), st("i2", Status.IDLE)])
    o.toggle_pin(AgentKey("dev", "i2"), 0)
    assert labels(o)[:3] == ["i2", "w1", "+1"]


def test_group_tile_is_not_a_preview_target():
    o, t = make([st("i1", Status.IDLE)])
    o.render()
    o.confirm_rendered_preview()
    t["now"] += 5
    assert o.agent_for_preview(0) is None


def test_tick_reports_working_tiles_with_the_group_present():
    o, _ = make([st("i1", Status.IDLE), st("w1", Status.WORKING), st("w2", Status.WORKING)])
    o.render()
    assert o.tick() == [0, 1]


def test_expanded_list_folds_back_after_the_idle_timeout():
    o, t = make([st("w1", Status.WORKING), st("i1", Status.IDLE)])
    o.on_press(1)
    assert labels(o)[1] == "i1"
    t["now"] += MENU_IDLE_TIMEOUT_S + 1
    assert o.consume_expired_panel_hold() is True
    assert labels(o)[1] == "+1"


def test_idle_agent_becoming_active_leaves_the_group():
    o, _ = make([st("i1", Status.IDLE), st("i2", Status.IDLE)])
    assert labels(o)[0] == "+2"
    o.apply_event("dev", st("i1", Status.WORKING))
    assert labels(o)[:2] == ["i1", "+1"]


def test_triage_position_lookup_skips_the_group():
    o, _ = make([st("i1", Status.IDLE), st("b1", Status.BLOCKED)])
    assert o.triage()[0].pane_id == "b1"
