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
