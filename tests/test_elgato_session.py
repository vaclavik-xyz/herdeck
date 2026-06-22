from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig
from herdeck.elgato.session import ElgatoSession, KeyRender
from herdeck.model import AgentKey, AgentState, Status


class FakeIcons:
    """Renders a tile to deterministic bytes encoding its visible content."""

    def render_tile_bytes(self, tile) -> bytes:
        return f"{tile.label}|{tile.color}|{tile.status_text}|{tile.repo}".encode()


def make_config():
    return Config(
        servers=[ServerConfig("dev", "ws://dev", "t")],
        profiles=dict(DEFAULT_PROFILES),
        overview_order=["dev"],
        grid=(5, 3),
    )


def state(pane, status, label="api"):
    s = AgentState(AgentKey("dev", pane), "claude", label, status)
    s.repo = label
    return s


def test_slots_render_leased_agents_by_ordinal():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_slots([("s1", (0, 0)), ("s0", (1, 0))])  # s0 is column 1, s1 column 0
    sess.apply_snapshot("dev", [state("p1", Status.WORKING, "alpha"),
                                state("p2", Status.BLOCKED, "beta")])

    rendered = sess.render_all()

    # Reading-order: s1 (col 0) = ordinal 0 = alpha; s0 (col 1) = ordinal 1 = beta
    assert isinstance(rendered["s1"], KeyRender)
    assert b"alpha" in rendered["s1"].image_png
    assert b"green" in rendered["s1"].image_png  # working
    assert b"beta" in rendered["s0"].image_png
    assert b"amber" in rendered["s0"].image_png  # blocked


def test_empty_slot_renders_blank():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_slots([("s0", (0, 0)), ("s1", (1, 0))])
    sess.apply_snapshot("dev", [state("p1", Status.IDLE)])
    rendered = sess.render_all()
    assert b"|dim|" in rendered["s1"].image_png  # ordinal 1 unleased -> blank/dim


def test_render_honors_theme_color_overrides():
    cfg = make_config()
    cfg.theme.colors = {**cfg.theme.colors, "working": "lime", "offline": "black"}
    sess = ElgatoSession(cfg, FakeIcons())
    sess.set_slots([("s0", (0, 0))])
    sess.apply_snapshot("dev", [state("p1", Status.WORKING, "alpha")])
    assert b"lime" in sess.render_all()["s0"].image_png  # custom status color
    sess.set_connection("dev", False)
    assert b"black" in sess.render_all()["s0"].image_png  # custom offline color


def test_single_blocked_agent_is_auto_selected():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_slots([("s0", (0, 0)), ("s1", (1, 0))])
    sess.apply_snapshot("dev", [state("p1", Status.WORKING), state("p2", Status.BLOCKED)])
    assert sess.selected() == AgentKey("dev", "p2")


def test_manual_selection_beats_auto_and_clears_when_agent_vanishes():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_slots([("s0", (0, 0))])
    sess.apply_snapshot("dev", [state("p1", Status.WORKING), state("p2", Status.BLOCKED)])
    sess.select(AgentKey("dev", "p1"))
    assert sess.selected() == AgentKey("dev", "p1")  # manual beats auto
    sess.apply_snapshot("dev", [state("p2", Status.BLOCKED)])  # p1 gone
    assert sess.selected() == AgentKey("dev", "p2")  # falls back to auto


def test_two_blocked_agents_do_not_auto_select():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_slots([("s0", (0, 0))])
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED), state("p2", Status.BLOCKED)])
    assert sess.selected() is None


from herdeck.layout import parse_options  # noqa: F401, E402  (ensures dependency exists)


def test_approve_disabled_until_prompt_read_then_enabled_for_binary():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_slots([("s0", (0, 0))])
    sess.set_action_keys([("a", "approve", (0, 2)), ("d", "deny", (1, 2)), ("t", "stop", (2, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED)])  # auto-selected, prompt unread

    assert sess.action_enabled("approve") is False  # unread
    sess.set_detection(AgentKey("dev", "p1"), "Proceed? (y/n)")
    assert sess.action_enabled("approve") is True
    assert sess.action_enabled("deny") is True
    assert sess.action_enabled("stop") is True  # selected + online


def test_approve_disabled_for_multi_option_prompt():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_action_keys([("a", "approve", (0, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED)])
    sess.set_detection(AgentKey("dev", "p1"), "Pick:\n1. Yes\n2. No\n3. Maybe")
    assert sess.action_enabled("approve") is False  # multi-option -> deck cannot answer


def test_stop_disabled_when_server_offline():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_action_keys([("t", "stop", (2, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.WORKING)])
    sess.select(AgentKey("dev", "p1"))
    assert sess.action_enabled("stop") is True
    sess.set_connection("dev", False)
    assert sess.action_enabled("stop") is False


def test_action_key_render_shows_target_identity():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_action_keys([("a", "approve", (0, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED, "myrepo")])
    sess.set_detection(AgentKey("dev", "p1"), "y/n")
    assert b"myrepo" in sess.render_all()["a"].image_png


def test_stale_prompt_does_not_re_enable_approve_after_status_change():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_action_keys([("a", "approve", (0, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED)])
    sess.set_detection(AgentKey("dev", "p1"), "y/n")
    assert sess.action_enabled("approve") is True
    sess.apply_event("dev", state("p1", Status.WORKING))  # left blocked -> prune
    sess.apply_event("dev", state("p1", Status.BLOCKED))  # re-blocked, prompt unread
    assert sess.action_enabled("approve") is False  # stale "y/n" must not re-enable it


def test_detection_cleared_on_disconnect_so_reconnect_needs_fresh_read():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_action_keys([("a", "approve", (0, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED)])
    sess.set_detection(AgentKey("dev", "p1"), "y/n")
    assert sess.action_enabled("approve") is True
    sess.set_connection("dev", False)  # server drops
    sess.set_connection("dev", True)   # ...and reconnects
    assert sess.action_enabled("approve") is False  # stale prompt gone; awaits a fresh read


def test_blocked_without_detection_skips_offline_servers():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED)])
    assert sess.blocked_without_detection() == [AgentKey("dev", "p1")]
    sess.set_connection("dev", False)
    assert sess.blocked_without_detection() == []  # no proactive read for a dead server


def test_whitespace_only_prompt_neither_enables_nor_counts_as_read():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_action_keys([("a", "approve", (0, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED)])
    sess.set_detection(AgentKey("dev", "p1"), "   \n  ")
    assert sess.action_enabled("approve") is False  # blank is not a real prompt
    # blank must NOT satisfy the read, or the proactive read would stop firing forever
    assert sess.blocked_without_detection() == [AgentKey("dev", "p1")]


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_arm_times_out():
    clk = FakeClock()
    sess = ElgatoSession(make_config(), FakeIcons(), clock=clk, arm_timeout=3.0)
    sess.apply_snapshot("dev", [state("p1", Status.WORKING)])
    sess.select(AgentKey("dev", "p1"))
    sess._arm()  # internal: first Stop press arms (Task 7 wires the press)
    assert sess.is_armed() is True
    clk.now = 3.5
    sess.tick()
    assert sess.is_armed() is False


def test_changing_selection_disarms():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.apply_snapshot("dev", [state("p1", Status.WORKING), state("p2", Status.WORKING)])
    sess.select(AgentKey("dev", "p1"))
    sess._arm()
    assert sess.is_armed() is True
    sess.select(AgentKey("dev", "p2"))
    assert sess.is_armed() is False


def test_is_armed_expires_without_explicit_tick():
    clk = FakeClock()
    sess = ElgatoSession(make_config(), FakeIcons(), clock=clk, arm_timeout=3.0)
    sess.apply_snapshot("dev", [state("p1", Status.WORKING)])
    sess.select(AgentKey("dev", "p1"))
    sess._arm()
    clk.now = 4.0
    assert sess.is_armed() is False  # lazy expiry, no tick() called


def test_arm_cleared_when_target_vanishes_and_does_not_resurrect():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.apply_snapshot("dev", [state("p1", Status.WORKING)])
    sess.select(AgentKey("dev", "p1"))
    sess._arm()
    assert sess.is_armed() is True
    sess.apply_snapshot("dev", [])  # p1 gone -> reconcile clears the arm
    assert sess.is_armed() is False
    sess.apply_snapshot("dev", [state("p1", Status.WORKING)])  # same key reappears
    assert sess.is_armed() is False  # stale arm must not resurrect


def test_armed_stop_key_renders_confirm_state():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_action_keys([("t", "stop", (2, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.WORKING)])
    sess.select(AgentKey("dev", "p1"))
    sess._arm()
    assert b"STOP?" in sess.render_all()["t"].image_png  # armed shows the confirm prompt


def test_arm_cleared_on_disconnect_and_not_resurrected_by_reconnect():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_action_keys([("t", "stop", (2, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.WORKING)])
    sess.select(AgentKey("dev", "p1"))
    sess._arm()
    assert sess.is_armed() is True
    sess.set_connection("dev", False)  # server drops while armed
    assert sess.is_armed() is False  # offline target disarms (stop is disabled offline)
    assert b"STOP?" not in sess.render_all()["t"].image_png  # tile no longer confirms
    sess.set_connection("dev", True)  # quick reconnect
    assert sess.is_armed() is False  # stale confirm must not resurrect


from herdeck.commands import Command  # noqa: E402


def test_pressing_slot_selects_and_reads():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_slots([("s0", (0, 0))])
    sess.apply_snapshot("dev", [state("p1", Status.WORKING)])
    cmds = sess.key_up("s0")
    assert cmds == [
        Command("focus", "dev", "p1"),
        Command("read", "dev", "p1", source="detection"),
    ]
    assert sess.selected() == AgentKey("dev", "p1")


def test_approve_emits_guarded_act_if_blocked():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_action_keys([("a", "approve", (0, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED)])
    sess.set_detection(AgentKey("dev", "p1"), "y/n")
    cmds = sess.key_up("a")
    assert cmds == [Command("act_if_blocked", "dev", "p1", keys=["1", "enter"])]


def test_stop_requires_arm_then_confirm():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_action_keys([("t", "stop", (2, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.WORKING)])
    sess.select(AgentKey("dev", "p1"))
    assert sess.key_up("t") == []          # first press arms
    assert sess.is_armed() is True
    assert sess.key_up("t") == [Command("act_force", "dev", "p1", keys=["ctrl+c"])]
    assert sess.is_armed() is False         # fired -> disarmed


def test_pager_advances_selection_through_blocked():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_action_keys([("p", "pager", (3, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED), state("p2", Status.BLOCKED)])
    assert sess.selected() is None  # two blocked, no auto
    sess.key_up("p")
    first = sess.selected()
    sess.key_up("p")
    assert sess.selected() != first  # cycled to the other blocked agent


def test_stop_confirm_after_timeout_rearms_instead_of_firing():
    clk = FakeClock()
    sess = ElgatoSession(make_config(), FakeIcons(), clock=clk, arm_timeout=3.0)
    sess.set_action_keys([("t", "stop", (2, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.WORKING)])
    sess.select(AgentKey("dev", "p1"))
    assert sess.key_up("t") == []  # first press arms
    clk.now = 4.0                  # confirm window expired, no manual tick()
    assert sess.key_up("t") == []  # must re-arm, NOT fire act_force
    assert sess.is_armed() is True


def test_repeated_approve_while_pending_does_not_double_send():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_action_keys([("a", "approve", (0, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED)])
    sess.set_detection(AgentKey("dev", "p1"), "y/n")
    first = sess.key_up("a")
    second = sess.key_up("a")  # pressed again before any state update
    assert len(first) == 1 and second == []  # no duplicate act_if_blocked


def test_pending_act_clears_on_next_snapshot():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_action_keys([("a", "approve", (0, 2))])
    sess.apply_snapshot("dev", [state("p1", Status.BLOCKED)])
    sess.set_detection(AgentKey("dev", "p1"), "y/n")
    sess.key_up("a")              # emits act, sets pending
    assert sess.key_up("a") == []  # suppressed while pending
    sess.apply_snapshot("dev", [state("p1", Status.IDLE)])  # the act's re-list result
    assert sess._pending_act is None  # pending cleared, key not stuck


def test_render_diff_returns_only_changed_instances():
    sess = ElgatoSession(make_config(), FakeIcons())
    sess.set_slots([("s0", (0, 0)), ("s1", (1, 0))])
    sess.apply_snapshot("dev", [state("p1", Status.WORKING, "a"), state("p2", Status.IDLE, "b")])

    first = sess.take_render_diff()
    assert set(first) == {"s0", "s1"}  # first call: everything

    assert sess.take_render_diff() == {}  # nothing changed

    sess.apply_event("dev", state("p1", Status.BLOCKED, "a"))  # p1 changed only
    diff = sess.take_render_diff()
    assert set(diff) == {"s0"}
