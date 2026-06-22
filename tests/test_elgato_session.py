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
