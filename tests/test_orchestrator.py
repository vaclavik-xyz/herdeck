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
    assert rs.panel.title == "▲ needs you"
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


def test_empty_slots_are_near_background():
    # "dim" (70,70,70) rendered vacant slots BRIGHTER than occupied tiles on
    # fill="none" — vacant must never outrank occupied (audit: empty-slot).
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [state("p1", Status.IDLE)])
    rs = o.render()
    assert rs.tiles[1].color == "empty" and rs.tiles[1].label == ""


def test_event_updates_tile():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [state("p1", Status.WORKING)])
    o.apply_event("dev", state("p1", Status.BLOCKED))
    assert o.render().tiles[0].color == "amber"


def test_event_recycled_terminal_starts_fresh_block_episode():
    now = [1.0]
    key = AgentKey("dev", "p1")
    o = Orchestrator(make_config(), slots=13, clock=lambda: now[0])
    o.apply_snapshot(
        "dev",
        [AgentState(key, "claude", "old", Status.BLOCKED, terminal_id="term-old")],
    )
    o._force_adopt = False
    now[0] = 10.0

    o.apply_event(
        "dev",
        AgentState(
            key,
            "codex",
            "new",
            Status.BLOCKED,
            terminal_id="term-new",
        ),
    )

    assert o._since[key] == (Status.BLOCKED, 10.0)
    assert o._force_adopt is True


def test_agent_for_preview_reads_rendered_slot_without_mutating_order_state():
    now = [10.0]
    o = Orchestrator(make_config(), slots=13, clock=lambda: now[0])
    o.apply_snapshot(
        "dev",
        [state("p1", Status.IDLE), state("p2", Status.WORKING)],
    )
    o.render()
    o.confirm_rendered_preview()  # establish the mapping the browser actually sees
    now[0] += 0.81
    expected = o._display_order[0]
    before = (
        list(o._display_order),
        dict(o._display_ranks),
        list(o._target_keys),
        o._target_since,
        o._force_adopt,
        dict(o._slot_changed_at),
        dict(o._rendered_preview_slots),
        dict(o._preview_slot_changed_at),
        o._page,
        o._drill,
    )
    agent = o.agent_for_preview(0)
    after = (
        list(o._display_order),
        dict(o._display_ranks),
        list(o._target_keys),
        o._target_since,
        o._force_adopt,
        dict(o._slot_changed_at),
        dict(o._rendered_preview_slots),
        dict(o._preview_slot_changed_at),
        o._page,
        o._drill,
    )
    assert agent is not None and agent.key == expected
    assert after == before


def test_agent_for_preview_respects_drill_menus_and_control_tiles():
    now = [10.0]
    o = Orchestrator(make_config(), slots=13, clock=lambda: now[0])
    target = state("p1", Status.IDLE)
    o.apply_snapshot("dev", [target])
    o.render()
    o.confirm_rendered_preview()
    now[0] += 0.81
    assert o.agent_for_preview(0) == target
    assert o.agent_for_preview(12) is None  # corner launcher
    assert all(o.agent_for_preview(index) is None for index in o._panel_indices())

    o._drill = target.key
    o.render()
    o.confirm_rendered_preview()
    now[0] += 0.81
    assert o.agent_for_preview(12) == target  # any tile means the drilled pane
    o._launcher = True
    o.render()
    o.confirm_rendered_preview()
    assert o.agent_for_preview(0) is None
    o._launcher = False
    o._profile_menu = True
    o.render()
    o.confirm_rendered_preview()
    assert o.agent_for_preview(0) is None


def test_agent_for_preview_rejects_a_slot_changed_during_long_press():
    now = [10.0]
    o = Orchestrator(make_config(), slots=13, clock=lambda: now[0])
    target = state("p1", Status.IDLE)
    o.apply_snapshot("dev", [target])
    o.render()
    o.confirm_rendered_preview()
    assert o.agent_for_preview(0) is None
    now[0] += 0.79
    assert o.agent_for_preview(0) is None
    now[0] += 0.02
    assert o.agent_for_preview(0) == target


def test_agent_for_preview_guards_blank_to_agent_render_change():
    now = [10.0]
    o = Orchestrator(make_config(), slots=13, clock=lambda: now[0])
    o.render()
    o.confirm_rendered_preview()

    target = state("p1", Status.IDLE)
    o.apply_snapshot("dev", [target])
    o.render()
    o.confirm_rendered_preview()

    assert o.agent_for_preview(0) is None
    now[0] += 0.81
    assert o.agent_for_preview(0) == target


def test_agent_for_preview_guards_successful_page_flip():
    now = [10.0]
    o = Orchestrator(make_config(), slots=13, clock=lambda: now[0])
    agents = [state(f"p{i:02}", Status.IDLE) for i in range(13)]
    o.apply_snapshot("dev", agents)
    o.render()
    o.confirm_rendered_preview()
    now[0] += 0.81
    first_page_agent = o.agent_for_preview(0)
    assert first_page_agent is not None

    o._page = 1
    o._resettle()
    o.render()
    o.confirm_rendered_preview()

    assert o.agent_for_preview(0) is None
    now[0] += 0.81
    second_page_agent = o.agent_for_preview(0)
    assert second_page_agent is not None
    assert second_page_agent.key != first_page_agent.key


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


def test_empty_server_accent_palette_disables_tile_accents():
    cfg = make_multi_config()
    cfg.theme.server_accents = []
    o = Orchestrator(cfg, slots=13)
    o.apply_snapshot(
        "alpha",
        [AgentState(AgentKey("alpha", "p1"), "claude", "ra", Status.IDLE)],
    )
    o.apply_event("bravo", AgentState(AgentKey("bravo", "p1"), "codex", "rb", Status.IDLE))

    tiles = [tile for tile in o.render().tiles if tile.repo]

    assert all(tile.server_tag for tile in tiles)
    assert all(tile.server_accent is None for tile in tiles)


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
    assert server_accent("alpha", []) is None


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
    assert tile.branch == "›2 · main"    # secondary = tab + branch


def test_overview_default_secondary_shows_tab_before_branch():
    cfg = make_config()
    o = Orchestrator(cfg, slots=13)
    s = AgentState(AgentKey("dev", "w2:p1"), "claude", "herdeck", Status.WORKING)
    s.repo, s.branch, s.tab = "herdeck", "main", "codex"
    o.apply_snapshot("dev", [s])

    tile = o.render().tiles[0]

    assert tile.repo == "herdeck"
    assert tile.branch == "›codex · main"


def test_overview_tile_lines_fall_back_to_tile_fields():
    # No explicit line config: tile_fields=["repo"] still hides tab and branch.
    cfg = make_multi_config()
    cfg.view.tile_fields = ["repo"]
    o = Orchestrator(cfg, slots=13)
    s = AgentState(AgentKey("alpha", "p1"), "claude", "api", Status.IDLE)
    s.repo, s.branch, s.tab = "repo", "feat/x", "hidden-tab"
    o.apply_snapshot("alpha", [s])

    tile = o.render().tiles[0]

    assert tile.repo == "repo"
    assert tile.branch == ""


def test_elapsed_seconds_quantized_to_5s_buckets():
    """Sub-minute elapsed is bucketed so the tile render cache is not defeated
    by a fresh signature every second (audit: elapsed-quantize)."""
    clk = [1000.0]
    o = Orchestrator(make_config(), slots=13, clock=lambda: clk[0])
    s = AgentState(AgentKey("dev", "p1"), "claude", "api", Status.WORKING)
    s.repo = "api"
    o.apply_snapshot("dev", [s])
    clk[0] = 1000.0 + 23
    assert o.render().tiles[0].time_text == "20s"
    clk[0] = 1000.0 + 24  # same bucket -> same text -> cache reuse
    assert o.render().tiles[0].time_text == "20s"
    clk[0] = 1000.0 + 25
    assert o.render().tiles[0].time_text == "25s"


# --- usage limits on the overview panel (CodexBar) ---


def _usage_data():
    from herdeck.usage import ProviderUsage, UsageWindow

    return [
        ProviderUsage("claude", [UsageWindow("5h", 19, None), UsageWindow("7d", 43, None)]),
        ProviderUsage("codex", [UsageWindow("5h", 2, None)]),
    ]


def test_overview_panel_carries_usage_lines():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [state("p1", Status.IDLE)])
    o.set_usage(_usage_data())
    rs = o.render()
    assert "Claude 5h 19% · 7d 43%" in rs.panel.lines
    assert "Codex 5h 2%" in rs.panel.lines
    assert [(g.label, g.window) for g in rs.panel.gauges] == [
        ("Claude", "5H"),
        ("Claude", "7D"),
        ("Codex", "5H"),
    ]


def test_overview_panel_without_usage_keeps_online_line():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [state("p1", Status.IDLE)])
    rs = o.render()
    assert rs.panel.lines[-1] == "online"


def test_panel_press_toggles_usage_detail_on_single_page():
    t = {"now": 0.0}
    o = Orchestrator(make_config(), slots=13, clock=lambda: t["now"])
    o.apply_snapshot("dev", [state("p1", Status.IDLE)])
    o.set_usage(_usage_data())
    assert o.on_press(13) == []
    rs = o.render()
    assert rs.panel.title == "usage limits"
    assert rs.panel.lines[0].startswith("Claude 5h 19%")
    assert rs.panel.gauges[0].label == "Claude"
    # second press hides the detail again
    o.on_press(13)
    assert o.render().panel.title == "1 agents"
    # expired hold reverts on its own
    o.on_press(13)
    t["now"] = 100.0
    assert o.render().panel.title == "1 agents"


def test_panel_press_still_pages_when_multipage():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot(
        "dev", [state(f"p{i}", Status.IDLE, label=f"a{i}") for i in range(30)]
    )
    o.set_usage(_usage_data())
    o.on_press(13)
    assert o._page == 1  # paging wins over the usage detail on multi-page decks


def test_blocked_spotlight_preempts_held_usage_detail():
    t = {"now": 0.0}
    o = Orchestrator(make_config(), slots=13, clock=lambda: t["now"])
    o.apply_snapshot("dev", [state("p1", Status.IDLE)])
    o.set_usage(_usage_data())
    o.on_press(13)
    assert o.render().panel.title == "usage limits"
    o.apply_snapshot("dev", [state("p1", Status.BLOCKED)])
    panel = o.render().panel
    assert panel.title == "▲ needs you"  # attention beats detail
    assert panel.gauges == []  # alert content must not be replaced by usage cards


def test_offline_panel_preempts_usage_gauges():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [state("p1", Status.IDLE)])
    o.set_usage(_usage_data())
    o.set_connection("dev", False)
    panel = o.render().panel
    assert panel.title == "OFFLINE"
    assert panel.gauges == []


def test_usage_detail_gauge_metadata_is_localized():
    cfg = make_config()
    cfg.view.language = "cs"
    o = Orchestrator(cfg, slots=13)
    o.apply_snapshot("dev", [state("p1", Status.IDLE)])
    o.set_usage(_usage_data())
    o.on_press(13)
    panel = o.render().panel
    assert panel.gauge_meta == "využito / obnova"


def test_usage_detail_pages_via_repeated_presses():
    from herdeck.usage import ProviderUsage, UsageWindow

    t = {"now": 0.0}
    o = Orchestrator(make_config(), slots=13, clock=lambda: t["now"])
    o.apply_snapshot("dev", [state("p1", Status.IDLE)])
    o.set_usage(
        [
            ProviderUsage("claude", [UsageWindow("5h", 1, None), UsageWindow("7d", 2, None)]),
            ProviderUsage("codex", [UsageWindow("5h", 3, None), UsageWindow("7d", 4, None)]),
        ]
    )
    o.on_press(13)
    rs = o.render()
    assert rs.panel.title.endswith("· 1/2")
    assert len(rs.panel.lines) == 3
    o.on_press(13)  # page 2 -> the 4th window's reset is reachable, not dropped
    rs = o.render()
    assert rs.panel.title.endswith("· 2/2")
    assert rs.panel.lines == ["Codex 7d 4%"]
    o.on_press(13)  # past the last page -> hide
    assert o.render().panel.title == "1 agents"


def test_expired_detail_hold_fires_consume_once():
    t = {"now": 0.0}
    o = Orchestrator(make_config(), slots=13, clock=lambda: t["now"])
    o.apply_snapshot("dev", [state("p1", Status.IDLE)])
    o.set_usage(_usage_data())
    o.on_press(13)
    assert o.consume_expired_panel_hold() is False  # still held
    t["now"] = 100.0
    assert o.consume_expired_panel_hold() is True  # hosts render on this signal
    assert o.consume_expired_panel_hold() is False  # one-shot
    assert o.render().panel.title == "1 agents"


def test_panel_press_does_not_arm_detail_during_spotlight():
    t = {"now": 0.0}
    o = Orchestrator(make_config(), slots=13, clock=lambda: t["now"])
    o.apply_snapshot("dev", [state("p1", Status.BLOCKED)])
    o.set_usage(_usage_data())
    o.on_press(13)  # spotlight owns the panel; the press must not arm a timer
    assert o._usage_detail_until == 0.0
    o.apply_snapshot("dev", [state("p1", Status.IDLE)])
    assert o.render().panel.title == "1 agents"  # no surprise detail pop-up


def test_tile_press_dismisses_held_detail():
    t = {"now": 0.0}
    o = Orchestrator(make_config(), slots=13, clock=lambda: t["now"])
    o.apply_snapshot("dev", [state("p1", Status.IDLE)])
    o.set_usage(_usage_data())
    o.on_press(13)
    assert o.render().panel.title == "usage limits"
    t["now"] = 1.0  # beyond the slot-press guard
    o.on_press(0)  # drilling an agent moves attention: the hold must not linger
    assert o._usage_detail_until == 0.0


def test_waiting_agent_renders_violet_with_holder_label():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot(
        "dev",
        [
            AgentState(
                AgentKey("dev", "p1"),
                "claude",
                "api",
                Status.WAITING,
                waiting_on="⏳ ci",
            ),
            state("p2", Status.WORKING),
        ],
    )
    rs = o.render()
    tiles = {t.label: t for t in rs.tiles if t.label}
    waiting = tiles["api"]
    assert waiting.color == "violet"
    assert waiting.status_text == "CI"  # the holder's label, not a generic word
    assert waiting.spinner is None  # waiting tiles do not animate
    working = [t for t in rs.tiles if t.color == "green"]
    assert working and rs.tiles.index(working[0]) < rs.tiles.index(waiting)  # working sorts first
