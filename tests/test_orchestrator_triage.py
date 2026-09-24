"""Triage loop: the NEEDS YOU panel press walks the blocked agents, longest
waiting first, and the external `triage()` entry point (desktop hotkey) does
the same."""

from herdeck.config import AnswerProfile, Config, ServerConfig
from herdeck.model import AgentKey, AgentState, Status
from herdeck.orchestrator import Command, Orchestrator

PROMPT = "Do you want to proceed?\n❯ 1. Yes\n  2. Yes, and don't ask again\n  3. No"
PANEL = 13  # first panel key on the 13-slot D200 layout
BACK = 12


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


def st(pane, status, label=None):
    return AgentState(AgentKey("dev", pane), "claude", label or pane, status)


def fleet():
    """p3 blocked first (t=10), then p1 (t=20), then p2 (t=30); p4 working."""
    t = {"now": 0.0}
    o = Orchestrator(make_config(), slots=13, clock=lambda: t["now"])
    o.apply_snapshot("dev", [st("p4", Status.WORKING)])
    for pane, at in (("p3", 10.0), ("p1", 20.0), ("p2", 30.0)):
        t["now"] = at
        o.apply_event("dev", st(pane, Status.BLOCKED))
    t["now"] = 100.0
    return o, t


def drill_cmds(pane):
    return [
        Command("focus", "dev", pane),
        Command("read", "dev", pane, source="detection"),
    ]


def test_panel_press_on_spotlight_opens_longest_blocked_drill():
    o, _ = fleet()
    assert o.render().panel.lines[0] == "p3"  # the spotlight names the oldest
    assert o.on_press(PANEL) == drill_cmds("p3")
    assert o.drill_key() == AgentKey("dev", "p3")


def test_answering_a_triage_drill_moves_to_next_longest_blocked():
    o, _ = fleet()
    o.on_press(PANEL)
    o.set_detection(PROMPT)
    cmds = o.on_press(0)
    assert cmds == [
        Command("act_if_blocked", "dev", "p3", keys=["1", "enter"]),
        *drill_cmds("p1"),
    ]
    assert o.drill_key() == AgentKey("dev", "p1")
    # the new drill starts clean: no stale prompt from the previous agent
    assert o.render().tiles[0].label != "1"


def test_triage_walks_every_blocked_agent_then_returns_to_overview():
    o, _ = fleet()
    o.on_press(PANEL)
    seen = []
    for _ in range(3):
        seen.append(o.drill_key().pane_id)
        o.set_detection(PROMPT)
        o.on_press(0)
    assert seen == ["p3", "p1", "p2"]
    assert not o.is_drilling()


def test_triage_skips_agents_that_unblocked_meanwhile():
    o, _ = fleet()
    o.on_press(PANEL)
    o.apply_event("dev", st("p1", Status.WORKING))  # answered from the phone
    o.set_detection(PROMPT)
    cmds = o.on_press(0)
    assert cmds[1:] == drill_cmds("p2")


def test_back_from_triage_drill_returns_to_overview():
    o, _ = fleet()
    o.on_press(PANEL)
    assert o.on_press(BACK) == []
    assert not o.is_drilling()


def test_stop_in_triage_drill_also_advances():
    o, _ = fleet()
    o.config.safety.require_confirm_for = []
    o.on_press(PANEL)
    cmds = o.on_press(11)  # Stop
    assert cmds[0].kind == "act_force" and cmds[0].pane_id == "p3"
    assert o.drill_key() == AgentKey("dev", "p1")


def test_tile_opened_drill_still_returns_to_overview_after_answer():
    o, t = fleet()
    t["now"] = 200.0
    o.render()
    o.on_press(0)  # a plain tile press, not triage
    o.set_detection(PROMPT)
    cmds = o.on_press(0)
    assert len(cmds) == 1
    assert not o.is_drilling()


def test_panel_press_without_blocked_agents_still_pages():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st(f"p{i:02}", Status.IDLE) for i in range(30)])
    assert o.on_press(PANEL) == []
    assert o._page == 1


def test_panel_press_opens_triage_even_on_multipage_deck():
    o, t = fleet()
    o.apply_snapshot(
        "dev",
        [*(o.get_agent(AgentKey("dev", p)) for p in ("p1", "p2", "p3", "p4"))]
        + [st(f"i{i:02}", Status.IDLE) for i in range(20)],
    )
    assert o.on_press(PANEL) == drill_cmds("p3")


def test_triage_entry_point_opens_oldest_and_cycles_on_repeat():
    o, _ = fleet()
    assert o.triage() == drill_cmds("p3")
    assert o.triage() == drill_cmds("p1")  # hotkey again = next blocked agent
    assert o.triage() == drill_cmds("p2")
    assert o.triage() == drill_cmds("p3")  # wraps around


def test_triage_entry_point_without_blocked_agents_is_a_noop():
    o = Orchestrator(make_config(), slots=13)
    o.apply_snapshot("dev", [st("p1", Status.IDLE)])
    assert o.triage() == []
    assert not o.is_drilling()


def test_triage_entry_point_leaves_launcher():
    o, _ = fleet()
    o.on_press(12)  # + New launcher
    assert o.triage() == drill_cmds("p3")
    assert o.render().tiles[BACK].label == "Back"
    assert o.drill_key() == AgentKey("dev", "p3")


def test_triage_ignores_inactive_lifecycle_blocked_agents():
    o, _ = fleet()
    stale = st("p3", Status.BLOCKED)
    stale.lifecycle = "archived"
    o.apply_event("dev", stale)
    assert o.triage() == drill_cmds("p1")


def test_triage_opens_t3_agent_with_a_read_only():
    o, _ = fleet()
    t3 = AgentState(AgentKey("dev", "p3"), "codex", "p3", Status.BLOCKED, backend="t3")
    o.apply_event("dev", t3)
    assert o.triage() == [Command("read", "dev", "p3")]


def test_overview_orders_blocked_tiles_longest_waiting_first():
    o, _ = fleet()
    labels = [t.label for t in o.render().tiles[:4]]
    assert labels == ["p3", "p1", "p2", "p4"]  # not pane-id order p1, p2, p3


def test_next_triage_drill_acknowledges_the_previous_answer():
    o, _ = fleet()
    o.on_press(PANEL)
    o.set_detection(PROMPT)
    o.on_press(0)
    assert o.render().panel.lines[0] == "sent › p3"


def test_idle_timeout_ends_the_triage_loop():
    from herdeck.orchestrator import MENU_IDLE_TIMEOUT_S

    o, t = fleet()
    o.on_press(PANEL)
    t["now"] += MENU_IDLE_TIMEOUT_S + 1
    assert o.consume_expired_panel_hold() is True
    assert not o.is_drilling()
    t["now"] += 1
    o.render()
    o.on_press(0)  # a later plain tile drill is not part of any triage run
    o.set_detection(PROMPT)
    assert len(o.on_press(0)) == 1
