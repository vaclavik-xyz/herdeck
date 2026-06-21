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
    assert rs.tiles[0].label.startswith("1 Yes")
    assert rs.tiles[1].label.startswith("2 Yes")
    assert rs.tiles[2].label.startswith("3 No")
    assert rs.tiles[11].label == "Stop" and rs.tiles[12].label == "Back"


def test_pressing_option_sends_its_number_and_returns_to_overview():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED)])
    o.on_press(0)
    o.set_detection(PROMPT)
    assert o.on_press(2) == [Command("act_if_blocked", "dev", "p1", keys=["3"])]
    assert not o.is_drilling()  # back on the fleet overview
    assert o.render().tiles[0].agent_type == "claude"


def test_pressing_option_one_sends_its_number():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED)])
    o.on_press(0)
    o.set_detection(PROMPT)
    assert o.on_press(0) == [Command("act_if_blocked", "dev", "p1", keys=["1"])]


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
    assert panel.title == "⚠ needs you"
    assert panel.lines[0] == "older"  # entered BLOCKED earliest
