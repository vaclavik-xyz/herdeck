from herdeck.config import AnswerProfile, Config, ServerConfig
from herdeck.driver.base import PanelView
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


def make_multi_config():
    return Config(
        servers=[],
        profiles={
            "default": AnswerProfile(["enter"], ["esc"], ["ctrl+c"], ["enter"]),
        },
        overview_order=["alpha", "bravo"],
        grid=(5, 3),
    )


def state(pane, status, agent_type="claude", label="api"):
    return AgentState(AgentKey("dev", pane), agent_type, label, status)


def test_overview_orders_blocked_first_and_colors():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [state("p1", Status.IDLE), state("p2", Status.BLOCKED)])
    rs = o.render()
    assert rs.tiles[0].color == "amber"  # blocked first
    assert rs.tiles[0].agent_type == "claude"
    assert rs.tiles[1].color == "blue"  # idle next
    assert isinstance(rs.panel, PanelView)


def test_done_sorts_above_working_and_renders_cyan():
    # done = finished but unseen -> surfaces at the top (below blocked, above
    # working/idle) as its own cyan tile, not buried after the idle agents.
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot(
        "dev",
        [
            state("p1", Status.IDLE),
            state("p2", Status.WORKING),
            state("p3", Status.DONE),
        ],
    )
    rs = o.render()
    assert rs.tiles[0].color == "cyan" and rs.tiles[0].status_text == "DONE"  # done first
    assert rs.tiles[1].color == "green"  # working next
    assert rs.tiles[2].color == "blue"  # idle last


def test_tile_fill_propagates_from_config_to_agent_tiles():
    cfg = make_config()
    cfg.view.tile_fill = "solid"
    o = Orchestrator(cfg, slots=13)
    o.apply_snapshot("dev", [state("p1", Status.WORKING)])
    rs = o.render()
    assert rs.tiles[0].tile_fill == "solid"  # agent tile carries the config value
    assert rs.tiles[1].tile_fill == "none"  # empty/control tiles keep the default


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
    clk[0] = 1000.0 + 185  # 3 minutes later
    t = o.render().tiles[0]
    assert t.repo == "macdoktor-crm" and t.branch == "feat/x"
    assert t.status_text == "WORKING" and t.time_text == "3m"


def test_multi_server_tiles_get_server_tag():
    o = Orchestrator(make_multi_config(), slots=13)
    o.apply_snapshot(
        "alpha",
        [
            AgentState(AgentKey("alpha", "p1"), "claude", "ra", Status.IDLE),
        ],
    )
    o.apply_event("bravo", AgentState(AgentKey("bravo", "p1"), "codex", "rb", Status.IDLE))

    tiles = [tile for tile in o.render().tiles if tile.repo]

    assert [tile.server_tag for tile in tiles] == ["ALP", "BRA"]
    assert all(tile.server_accent for tile in tiles)


def test_multi_server_tags_stay_visible_on_single_server_page():
    o = Orchestrator(make_multi_config(), slots=3)
    o.apply_snapshot(
        "alpha",
        [
            AgentState(AgentKey("alpha", "p1"), "claude", "ra1", Status.IDLE),
            AgentState(AgentKey("alpha", "p2"), "claude", "ra2", Status.IDLE),
        ],
    )
    o.apply_snapshot(
        "bravo",
        [
            AgentState(AgentKey("bravo", "p1"), "codex", "rb", Status.IDLE),
        ],
    )

    tiles = [tile for tile in o.render().tiles if tile.repo]

    assert [tile.server_tag for tile in tiles] == ["ALP", "ALP"]
    assert all(tile.server_accent for tile in tiles)


def test_single_server_tiles_have_no_tag():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [state("p1", Status.IDLE)])

    tiles = [tile for tile in o.render().tiles if tile.repo]

    assert all(tile.server_tag is None and tile.server_accent is None for tile in tiles)


def test_server_accent_returns_stable_palette_color():
    from herdeck.orchestrator import SERVER_ACCENTS, server_accent

    assert server_accent("alpha") == server_accent("alpha")
    assert server_accent("alpha") in SERVER_ACCENTS


def test_theme_status_colors_apply_to_agent_tiles():
    cfg = make_config()
    cfg.theme.colors["blocked"] = "pink"
    o = Orchestrator(cfg, slots=13)
    o.apply_snapshot("dev", [state("p1", Status.BLOCKED)])

    assert o.render().tiles[0].color == "pink"


def test_theme_status_colors_apply_to_overview_panel():
    cfg = make_config()
    cfg.theme.colors["blocked"] = "pink"
    cfg.theme.colors["offline"] = "violet"
    o = Orchestrator(cfg, slots=13)
    o.apply_snapshot("dev", [state("p1", Status.BLOCKED)])

    assert o.render().panel.color == "pink"

    o.set_connection("dev", False)

    assert o.render().panel.color == "violet"


def test_tile_fields_can_hide_branch_status_time_and_server_tag():
    cfg = make_multi_config()
    cfg.view.tile_fields = ["repo"]
    o = Orchestrator(cfg, slots=13)
    s = AgentState(AgentKey("alpha", "p1"), "claude", "api", Status.IDLE)
    s.repo = "repo"
    s.branch = "feat/x"
    o.apply_snapshot("alpha", [s])
    o.apply_event("bravo", AgentState(AgentKey("bravo", "p1"), "codex", "rb", Status.IDLE))

    tile = o.render().tiles[0]

    assert tile.repo == "repo"
    assert tile.branch == ""
    assert tile.status_text is None
    assert tile.time_text is None
    assert tile.server_tag is None


def test_overview_renders_configured_tile_lines():
    cfg = make_config()
    cfg.view.tile_primary = ["workspace"]
    cfg.view.tile_secondary = ["tab", "branch"]
    o = Orchestrator(cfg, slots=13)
    # distinct repo vs workspace so a stale "render repo as primary" impl fails
    s = AgentState(AgentKey("dev", "w2:p1"), "claude", "herdeck", Status.WORKING)
    s.repo, s.branch, s.workspace, s.tab = "api", "main", "herdeck", "2"
    o.apply_snapshot("dev", [s])

    tile = o.render().tiles[0]

    assert tile.repo == "herdeck"        # primary = workspace, NOT repo "api"
    assert tile.branch == "▸2 · main"    # secondary = tab + branch


def test_overview_tile_lines_fall_back_to_tile_fields():
    # No new keys set; tile_fields=["repo"] must still hide branch (today's behavior).
    cfg = make_multi_config()
    cfg.view.tile_fields = ["repo"]
    o = Orchestrator(cfg, slots=13)
    s = AgentState(AgentKey("alpha", "p1"), "claude", "api", Status.IDLE)
    s.repo, s.branch = "repo", "feat/x"
    o.apply_snapshot("alpha", [s])

    tile = o.render().tiles[0]

    assert tile.repo == "repo"
    assert tile.branch == ""
