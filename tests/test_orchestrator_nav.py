from herdeck.config import AnswerProfile, Config, ServerConfig
from herdeck.model import AgentKey, AgentState, Status
from herdeck.orchestrator import Command, Orchestrator


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


# A blocked-agent prompt with numbered choices (e.g. Claude permission menu).
PROMPT = "Do you want to proceed?\n❯ 1. Yes\n  2. Yes, and don't ask again\n  3. No"


def test_tap_agent_focuses_drills_and_reads():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED)])
    cmds = o.on_press(0)
    assert cmds == [Command("focus", "dev", "p1"), Command("read", "dev", "p1", source="detection")]
    rs = o.render()
    # Before the read returns there are no options yet — only fixed Stop/Back.
    assert rs.tiles[11].label == "Stop" and rs.tiles[12].label == "Back"


def test_drill_shows_parsed_options_after_read():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED)])
    o.on_press(0)
    o.set_detection(PROMPT)
    rs = o.render()
    assert rs.tiles[0].label == "1" and rs.tiles[0].subtext == "Yes"
    assert rs.tiles[1].label == "2" and rs.tiles[1].subtext == "Yes, and don't ask again"
    assert rs.tiles[2].label == "3" and rs.tiles[2].subtext == "No"
    assert rs.tiles[11].label == "Stop" and rs.tiles[12].label == "Back"


def test_pressing_option_sends_its_number_and_returns_to_overview():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED)])
    o.on_press(0)
    o.set_detection(PROMPT)
    assert o.on_press(2) == [Command("act_if_blocked", "dev", "p1", keys=["3", "enter"])]
    assert not o.is_drilling()  # back on the fleet overview
    assert o.render().tiles[0].agent_type == "claude"


def test_pressing_option_one_sends_its_number_and_enter():
    # A numbered menu needs the digit AND Enter to confirm (the digit alone only
    # moves the selection); send both so the choice actually submits.
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED)])
    o.on_press(0)
    o.set_detection(PROMPT)
    assert o.on_press(0) == [Command("act_if_blocked", "dev", "p1", keys=["1", "enter"])]


def test_drill_option_tiles_carry_number_as_label_and_text_as_subtext():
    # The tile shows the choice NUMBER big (label) and the choice TEXT small
    # underneath (subtext, wraps) instead of a truncated "1 Yes…" single line.
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED)])
    o.on_press(0)
    o.set_detection(PROMPT)
    rs = o.render()
    assert rs.tiles[0].label == "1"
    assert rs.tiles[0].subtext == "Yes"
    assert rs.tiles[1].label == "2"
    assert rs.tiles[1].subtext == "Yes, and don't ask again"
    assert rs.tiles[2].label == "3"
    assert rs.tiles[2].subtext == "No"


def test_drill_approve_deny_fallback_tiles_have_no_subtext():
    # The y/n fallback (Approve/Deny) keeps its short word label, no subtext.
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED)])
    o.on_press(0)
    o.set_detection("Allow this edit?")  # read completed, no numbered options
    rs = o.render()
    assert rs.tiles[0].label == "Approve"
    assert rs.tiles[0].subtext is None


def test_drill_panel_shows_detail_after_read_result():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED)])
    o.on_press(0)
    o.set_detection("Allow edit?")
    assert "Allow edit?" in o.render().panel.lines[0]


def test_non_blocked_shows_macros_not_parsed_options():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.WORKING)])
    o.on_press(0)  # drill into a working agent
    o.set_detection(PROMPT)  # a prompt is present but agent isn't blocked
    rs = o.render()
    labels = [t.label for t in rs.tiles[:4]]
    assert "1 Yes" not in labels  # prompt options are NOT offered (not blocked)
    assert rs.tiles[0].label == "continue"  # macros are shown instead


def test_stop_works_even_when_not_blocked_and_returns_to_overview():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.WORKING)])
    o.on_press(0)
    # Stop is confirm-guarded by default now: the first press arms, the second fires.
    assert o.on_press(11) == []
    assert o.on_press(11) == [Command("act_force", "dev", "p1", keys=["ctrl+c"])]
    assert not o.is_drilling()


def test_back_returns_to_overview():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED)])
    o.on_press(0)
    assert o.on_press(12) == []
    assert o.render().tiles[0].agent_type == "claude"  # overview again


def test_panel_press_pages_in_overview():
    o = Orchestrator(make_config(), slots=3)  # 2 agent slots + launcher -> paging
    o.apply_snapshot("dev", [st("p1", Status.IDLE), st("p2", Status.IDLE), st("p3", Status.IDLE)])
    first = [t.label for t in o.render().tiles]
    assert o.on_press(3) == []  # panel press (slots) -> next page, no command
    second = [t.label for t in o.render().tiles]
    assert first != second
    assert o.render().panel.title == "3 agents"
    assert o.render().panel.lines[-1].endswith(" · 2/2")


def test_panel_indices_scale_with_slot_count():
    o = Orchestrator(make_config(), slots=4)  # 3 agent slots + launcher -> paging
    o.apply_snapshot(
        "dev",
        [
            st("p1", Status.IDLE),
            st("p2", Status.IDLE),
            st("p3", Status.IDLE),
            st("p4", Status.IDLE),
        ],
    )
    first = [t.label for t in o.render().tiles]
    assert o.on_press(13) == []  # legacy index 13 no longer pages a 4-slot deck
    assert [t.label for t in o.render().tiles] == first
    assert o.on_press(4) == []  # computed panel index (slots) pages instead
    assert [t.label for t in o.render().tiles] != first
    assert o.render().panel.title == "4 agents"
    assert o.render().panel.lines[-1].endswith(" · 2/2")


def test_new_tile_opens_launcher_and_starts_agent():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.IDLE)])
    assert o.render().tiles[12].label == "+ New"  # reserved launcher tile
    assert o.on_press(12) == []  # opens the launcher
    rs = o.render()
    assert rs.tiles[0].label == "claude" and rs.tiles[1].label == "codex"
    assert rs.tiles[12].label == "Back"
    assert o.on_press(0) == [Command("start", "dev", text="claude", keys=["claude"])]
    assert o.render().tiles[12].label == "+ New"  # back on overview


def test_launcher_back_returns_to_overview():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.IDLE)])
    o.on_press(12)  # open launcher
    assert o.on_press(12) == []  # Back
    assert o.render().tiles[12].label == "+ New"


def test_macros_shown_and_sent_for_non_blocked_agent():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.WORKING)])
    o.on_press(0)  # drill into a working agent
    rs = o.render()
    # macros from config fill the action tiles
    assert rs.tiles[0].label == "continue"
    # pressing a macro sends its text and returns to overview
    cmds = o.on_press(0)
    assert cmds == [Command("send_text", "dev", "p1", text="continue")]
    assert not o.is_drilling()


def test_blocked_without_numbered_options_falls_back_to_profile():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED, agent_type="claude")])
    o.on_press(0)
    o.set_detection("Proceed? (y/n)")  # no numbered options to parse
    rs = o.render()
    assert [t.label for t in rs.tiles[:3]] == ["Approve", "Approve!", "Deny"]
    assert o.on_press(0) == [
        Command("act_if_blocked", "dev", "p1", keys=["1", "enter"])
    ]  # claude approve seq
    assert not o.is_drilling()


def test_no_fallback_actions_before_read_completes():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED, agent_type="claude")])
    o.on_press(0)  # drilled, detection still empty
    rs = o.render()
    assert rs.tiles[0].label == ""  # no actions yet (no blind approve)
    assert o.on_press(0) == []


def test_safety_can_hide_approve_always_action():
    o = Orchestrator(make_config(), slots=13)
    o.config.safety.approve_always = False
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED, agent_type="claude")])
    o.on_press(0)
    o.set_detection("Proceed? (y/n)")

    labels = [t.label for t in o.render().tiles[:3]]

    assert labels == ["Approve", "Deny", ""]


def test_safety_can_hide_parsed_approve_always_option():
    o = Orchestrator(make_config(), slots=13)
    o.config.safety.approve_always = False
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED, agent_type="claude")])
    o.on_press(0)
    o.set_detection(PROMPT)

    labels = [t.label for t in o.render().tiles[:3]]

    assert all(not label.startswith("2 ") for label in labels)


def test_safety_can_hide_parsed_approve_always_by_label():
    cfg = make_config()
    cfg.profiles["codex"] = AnswerProfile(["y", "enter"], ["n", "enter"], ["ctrl+c"], ["y", "enter"])
    cfg.safety.approve_always = False
    o = Orchestrator(cfg, slots=13)
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED, agent_type="codex")])
    o.on_press(0)
    o.set_detection(PROMPT)

    labels = [t.label for t in o.render().tiles[:3]]

    assert all(not label.startswith("2 ") for label in labels)


def test_safety_confirmation_blocks_force_stop_until_second_press():
    o = Orchestrator(make_config(), slots=13)
    o.config.safety.require_confirm_for = ["act_force"]
    o.apply_snapshot("dev", [st("p1", Status.WORKING)])
    o.on_press(0)

    first = o.on_press(11)
    second = o.on_press(11)

    assert first == []
    assert second == [Command("act_force", "dev", "p1", keys=["ctrl+c"])]


def test_safety_confirmation_blocks_approve_always_until_second_press():
    o = Orchestrator(make_config(), slots=13)
    o.config.safety.require_confirm_for = ["approve_always"]
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED, agent_type="claude")])
    o.on_press(0)
    o.set_detection("Proceed? (y/n)")

    first = o.on_press(1)
    second = o.on_press(1)

    assert first == []
    assert second == [Command("act_if_blocked", "dev", "p1", keys=["2", "enter"])]


def test_safety_confirmation_blocks_parsed_approve_always_until_second_press():
    o = Orchestrator(make_config(), slots=13)
    o.config.safety.require_confirm_for = ["approve_always"]
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED, agent_type="claude")])
    o.on_press(0)
    o.set_detection(PROMPT)

    first = o.on_press(1)
    second = o.on_press(1)

    assert first == []
    assert second == [Command("act_if_blocked", "dev", "p1", keys=["2", "enter"])]


def test_safety_confirmation_blocks_label_based_approve_always_until_second_press():
    cfg = make_config()
    cfg.profiles["codex"] = AnswerProfile(["y", "enter"], ["n", "enter"], ["ctrl+c"], ["y", "enter"])
    cfg.safety.require_confirm_for = ["approve_always"]
    o = Orchestrator(cfg, slots=13)
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED, agent_type="codex")])
    o.on_press(0)
    o.set_detection(PROMPT)

    first = o.on_press(1)
    second = o.on_press(1)

    assert first == []
    assert second == [Command("act_if_blocked", "dev", "p1", keys=["2", "enter"])]


def test_safety_confirmation_resets_when_detection_changes():
    o = Orchestrator(make_config(), slots=13)
    o.config.safety.require_confirm_for = ["approve_always"]
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED, agent_type="claude")])
    o.on_press(0)
    o.set_detection(PROMPT)

    assert o.on_press(1) == []
    o.set_detection("Proceed?\n1. Yes\n2. Yes, always\n3. No")

    assert o.on_press(1) == []
    assert o.on_press(1) == [Command("act_if_blocked", "dev", "p1", keys=["2", "enter"])]


def test_safety_keeps_approve_when_approve_always_shares_key():
    cfg = make_config()
    cfg.profiles["claude"] = AnswerProfile(["1"], ["3"], ["ctrl+c"], ["1"])
    cfg.safety.approve_always = False
    o = Orchestrator(cfg, slots=13)
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED, agent_type="claude")])
    o.on_press(0)
    o.set_detection(PROMPT)

    assert o.render().tiles[0].label == "1"
    assert o.on_press(0) == [Command("act_if_blocked", "dev", "p1", keys=["1", "enter"])]


def test_safety_confirmation_survives_unchanged_snapshot():
    o = Orchestrator(make_config(), slots=13)
    o.config.safety.require_confirm_for = ["approve_always"]
    state = st("p1", Status.BLOCKED, agent_type="claude")
    o.apply_snapshot("dev", [state])
    o.on_press(0)
    o.set_detection(PROMPT)

    assert o.on_press(1) == []
    o.apply_snapshot("dev", [state])

    assert o.on_press(1) == [Command("act_if_blocked", "dev", "p1", keys=["2", "enter"])]


def test_safety_confirmation_survives_unchanged_event():
    o = Orchestrator(make_config(), slots=13)
    o.config.safety.require_confirm_for = ["approve_always"]
    state = st("p1", Status.BLOCKED, agent_type="claude")
    o.apply_snapshot("dev", [state])
    o.on_press(0)
    o.set_detection(PROMPT)

    assert o.on_press(1) == []
    o.apply_event("dev", state)

    assert o.on_press(1) == [Command("act_if_blocked", "dev", "p1", keys=["2", "enter"])]


def test_overview_panel_spotlights_oldest_blocked():
    from herdeck.config import AnswerProfile, Config
    from herdeck.model import AgentKey, AgentState, Status
    from herdeck.orchestrator import Orchestrator

    cfg = Config(
        servers=[],
        profiles={"default": AnswerProfile(["enter"], ["esc"], ["ctrl+c"], ["enter"])},
        overview_order=["s"],
        grid=(5, 3),
    )
    now = [0.0]
    orch = Orchestrator(cfg, slots=13, clock=lambda: now[0])
    now[0] = 100.0
    orch.apply_event("s", AgentState(AgentKey("s", "p1"), "claude", "older", Status.BLOCKED))
    now[0] = 200.0
    orch.apply_event("s", AgentState(AgentKey("s", "p2"), "claude", "newer", Status.BLOCKED))
    now[0] = 260.0
    panel = orch.render().panel
    assert panel.title == "▲ needs you"
    assert panel.lines[0] == "older"  # entered BLOCKED earliest


def test_launcher_contains_profiles_entry_when_multiple_profiles_exist():
    cfg = make_config()
    cfg.meta.profile_names = ["work", "mobile"]
    cfg.meta.active_profile = "work"
    o = Orchestrator(cfg, slots=13)

    o.on_press(12)  # + New
    labels = [t.label for t in o.render().tiles if t.label]

    assert "Profiles" in labels


def test_profile_menu_lists_profiles_and_switches():
    cfg = make_config()
    cfg.meta.profile_names = ["work", "mobile"]
    cfg.meta.active_profile = "work"
    o = Orchestrator(cfg, slots=13)

    o.on_press(12)  # + New
    profiles_index = [t.label for t in o.render().tiles].index("Profiles")
    assert o.on_press(profiles_index) == []
    rs = o.render()
    assert rs.panel.title == "profiles"
    assert rs.tiles[0].label == "* work"
    assert rs.tiles[1].label == "mobile"

    assert o.on_press(1) == [Command("switch_profile", "mobile", text="mobile")]
    assert o.render().tiles[12].label == "+ New"


def test_management_row_can_expose_profiles_and_new_agent():
    cfg = make_config()
    cfg.meta.profile_names = ["work", "mobile"]
    cfg.meta.active_profile = "work"
    cfg.view.management = "bottom_row"
    cfg.view.bottom_row = ["profiles", "new_agent"]
    o = Orchestrator(cfg, slots=13)
    o.apply_snapshot("dev", [st(f"p{i}", Status.IDLE, label=f"a{i}") for i in range(1, 13)])

    labels = [t.label for t in o.render().tiles]

    assert labels[10] == "Profiles"
    assert labels[11] == "+ New"
    assert labels[12] == ""


def test_profile_menu_back_from_management_row_returns_to_overview():
    cfg = make_config()
    cfg.meta.profile_names = ["work", "mobile"]
    cfg.view.management = "bottom_row"
    cfg.view.bottom_row = ["profiles"]
    o = Orchestrator(cfg, slots=13)

    assert o.on_press(10) == []
    assert o.render().panel.title == "profiles"
    assert o.on_press(12) == []

    rs = o.render()
    assert rs.panel.title == "0 agents"
    assert rs.tiles[10].label == "Profiles"


def test_management_row_does_not_capture_panel_keys_when_actions_overflow():
    cfg = make_config()
    cfg.meta.profile_names = ["work", "mobile"]
    cfg.view.management = "bottom_row"
    cfg.view.bottom_row = ["profiles", "notifications", "safety", "theme", "new_agent"]
    o = Orchestrator(cfg, slots=13)
    o.apply_snapshot("dev", [st(f"p{i}", Status.IDLE, label=f"a{i}") for i in range(1, 21)])

    assert o.render().panel.lines[-1].endswith(" · 1/2")
    assert o.on_press(13) == []
    assert o.render().panel.lines[-1].endswith(" · 2/2")


def test_management_row_keeps_new_agent_visible_when_actions_overflow():
    cfg = make_config()
    cfg.view.management = "bottom_row"
    cfg.view.bottom_row = ["profiles", "notifications", "safety", "theme", "new_agent"]
    o = Orchestrator(cfg, slots=13)

    labels = [t.label for t in o.render().tiles]

    assert labels[12] == "+ New"


def test_management_row_hides_unhandled_actions():
    cfg = make_config()
    cfg.view.management = "bottom_row"
    cfg.view.bottom_row = ["profiles", "notifications", "safety", "theme", "new_agent"]
    o = Orchestrator(cfg, slots=13)

    labels = [t.label for t in o.render().tiles]

    assert "Notify" not in labels
    assert "Safety" not in labels
    assert "Theme" not in labels


def test_update_config_prunes_removed_server_state():
    from herdeck.config import ServerConfig
    from herdeck.model import AgentKey, AgentState

    cfg = make_config()
    cfg.servers = [
        ServerConfig("old", "ws://old", "token"),
        ServerConfig("dev", "ws://dev", "token"),
    ]
    cfg.overview_order = ["old", "dev"]
    o = Orchestrator(cfg, slots=13)
    o.apply_snapshot(
        "old",
        [AgentState(AgentKey("old", "p1"), "codex", "old agent", Status.IDLE)],
    )

    next_cfg = make_config()
    next_cfg.servers = [ServerConfig("dev", "ws://dev", "token")]
    next_cfg.overview_order = ["dev"]
    o.update_config(next_cfg)

    assert "old agent" not in [t.label for t in o.render().tiles]


# --- armed-confirmation visibility + expiry (audit: armed-confirm-feedback) --


def test_armed_stop_confirmation_is_visible_and_expires():
    clk = [1000.0]
    o = Orchestrator(make_config(), slots=13, clock=lambda: clk[0])
    o.config.safety.require_confirm_for = ["act_force"]
    o.apply_snapshot("dev", [st("p1", Status.WORKING)])
    o.on_press(0)
    stop_i = o.slots - 2
    assert o.render().tiles[stop_i].label == "Stop"
    assert o.on_press(stop_i) == []  # first press arms
    rs = o.render()
    assert rs.tiles[stop_i].label == "Sure?"  # armed state is visible
    assert rs.panel.lines[0] == "press again to confirm"
    clk[0] += 10  # TTL expired: the stale arm must not complete
    assert o.render().tiles[stop_i].label == "Stop"  # visual arm cleared
    assert o.on_press(stop_i) == []  # re-arms instead of firing
    assert o.on_press(stop_i) == [Command("act_force", "dev", "p1", keys=["ctrl+c"])]


def test_armed_option_confirmation_marks_only_that_option_tile():
    o = Orchestrator(make_config(), slots=13)
    o.config.safety.require_confirm_for = ["approve_always"]
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED, agent_type="claude")])
    o.on_press(0)
    o.set_detection(PROMPT)
    assert o.on_press(1) == []  # arms the approve_always option
    tiles = o.render().tiles
    assert tiles[1].label == "Sure?"
    assert tiles[0].label != "Sure?"
    assert o.render().panel.lines[0] == "press again to confirm"


# --- drill action colour semantics (audit: drill-option-colors) --------------


def test_drill_option_tiles_carry_action_colors():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED, agent_type="claude")])
    o.on_press(0)
    o.set_detection(PROMPT)
    tiles = o.render().tiles
    assert tiles[0].color == "green"  # 1. Yes -> approve
    assert tiles[1].color == "amber"  # 2. Yes, and don't ask again -> caution
    assert tiles[2].color == "red"  # 3. No -> deny


def test_drill_fallback_actions_carry_action_colors():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED, agent_type="claude")])
    o.on_press(0)
    o.set_detection("Proceed? (y/n)")
    tiles = o.render().tiles
    assert [tiles[0].color, tiles[1].color, tiles[2].color] == ["green", "amber", "red"]


def test_drill_macro_tiles_stay_blue():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.WORKING)])
    o.on_press(0)
    assert o.render().tiles[0].color == "blue"  # macros carry no action semantics


# --- ordering hysteresis (audit: resort-hysteresis) ---------------------------


def _clocked(clk):
    return Orchestrator(make_config(), slots=13, clock=lambda: clk[0])


def test_order_holds_positions_until_target_settles():
    clk = [1000.0]
    o = _clocked(clk)
    o.apply_snapshot("dev", [st("p1", Status.WORKING, label="one"), st("p2", Status.IDLE, label="two")])
    assert [t.label for t in o.render().tiles[:2]] == ["one", "two"]
    o.apply_event("dev", st("p2", Status.BLOCKED, label="two"))
    # blocked would sort first, but tiles must not shuffle under the finger
    assert [t.label for t in o.render().tiles[:2]] == ["one", "two"]
    clk[0] += 2.5  # target stable past the settle window -> adopt the sort
    assert [t.label for t in o.render().tiles[:2]] == ["two", "one"]


def test_new_agent_appends_at_end_until_settle():
    clk = [1000.0]
    o = _clocked(clk)
    o.apply_snapshot("dev", [st("p1", Status.WORKING, label="one"), st("p2", Status.IDLE, label="two")])
    o.render()
    o.apply_snapshot(
        "dev",
        [
            st("p1", Status.WORKING, label="one"),
            st("p2", Status.IDLE, label="two"),
            st("p3", Status.BLOCKED, label="three"),
        ],
    )
    assert [t.label for t in o.render().tiles[:3]] == ["one", "two", "three"]
    clk[0] += 2.5
    assert [t.label for t in o.render().tiles[:3]] == ["three", "one", "two"]


def test_removed_agent_drops_immediately_while_positions_hold():
    clk = [1000.0]
    o = _clocked(clk)
    o.apply_snapshot(
        "dev",
        [
            st("p1", Status.WORKING, label="one"),
            st("p2", Status.WORKING, label="two"),
            st("p3", Status.IDLE, label="three"),
        ],
    )
    o.render()
    o.apply_snapshot(
        "dev", [st("p1", Status.WORKING, label="one"), st("p3", Status.IDLE, label="three")]
    )
    assert [t.label for t in o.render().tiles[:2]] == ["one", "three"]


def test_press_on_freshly_reshuffled_slot_is_swallowed():
    clk = [1000.0]
    o = _clocked(clk)
    o.apply_snapshot("dev", [st("p1", Status.WORKING, label="one"), st("p2", Status.IDLE, label="two")])
    o.render()
    o.apply_event("dev", st("p2", Status.BLOCKED, label="two"))
    o.render()  # production refreshes on every event; the settle timer starts here
    clk[0] += 2.5
    o.render()  # adoption: slot 0 repopulates (one -> two) right now
    assert o.on_press(0) == []  # press lands within the guard window -> swallowed
    clk[0] += 0.5
    cmds = o.on_press(0)  # deliberate second press drills the visible agent
    assert cmds and cmds[0].pane_id == "p2"
