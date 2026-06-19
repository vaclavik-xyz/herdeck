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


# A blocked-agent prompt with numbered choices (e.g. Claude permission menu).
PROMPT = "Do you want to proceed?\n❯ 1. Yes\n  2. Yes, and don't ask again\n  3. No"


def test_tap_agent_focuses_drills_and_reads():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.BLOCKED)])
    cmds = o.on_press(0)
    assert cmds == [Command("focus", "dev", "p1"),
                    Command("read", "dev", "p1", source="detection")]
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
    assert not o.is_drilling()                          # back on the fleet overview
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


def test_no_options_when_not_blocked():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.WORKING)])
    o.on_press(0)                       # drill into a working agent
    o.set_detection(PROMPT)             # even with a prompt, not blocked -> no options
    rs = o.render()
    assert rs.tiles[0].label == ""      # no option tiles
    assert o.on_press(0) == []          # pressing a blank tile does nothing


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
    o = Orchestrator(make_config(), slots=2)   # tiny deck -> paging
    o.apply_snapshot("dev", [st("p1", Status.IDLE), st("p2", Status.IDLE),
                             st("p3", Status.IDLE)])
    first = [t.label for t in o.render().tiles]
    assert o.on_press(13) == []        # panel press -> next page, no command
    second = [t.label for t in o.render().tiles]
    assert first != second
    assert o.render().panel.title == "page 2/2"
