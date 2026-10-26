"""The Elgato plugin session stamps the deck theme on every key it renders
and marks its action keys as controls (they are drawn by Theme.agent_tile
because they carry the target's name in ``repo``)."""

from __future__ import annotations

import pytest

from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig
from herdeck.elgato.session import ElgatoSession
from herdeck.model import AgentKey, AgentState, Status
from herdeck.project_icons import ProjectIconStore
from herdeck.themes import registry as reg_mod
from herdeck.themes.registry import ThemeRegistry


class _RecordingIcons:
    def __init__(self):
        self.tiles = []

    def render_tile_bytes(self, tile) -> bytes:
        self.tiles.append(tile)
        return repr((tile.label, tile.kind)).encode()


@pytest.fixture
def mine_theme(tmp_path):
    (tmp_path / "mine.py").write_text(
        "from herdeck.themes.api import Theme\n\n\n"
        "class T(Theme):\n    id = 'mine'\n\n\nTHEME = T()\n",
        encoding="utf-8",
    )
    prev = reg_mod.default_registry()
    reg_mod.set_default_registry(ThemeRegistry(str(tmp_path)))
    try:
        yield
    finally:
        reg_mod.set_default_registry(prev)


def _session(theme: str = "mine"):
    cfg = Config(
        servers=[ServerConfig("dev", "ws://dev", "t")],
        profiles=dict(DEFAULT_PROFILES),
        overview_order=["dev"],
        grid=(5, 3),
    )
    cfg.view.deck_theme = theme
    icons = _RecordingIcons()
    sess = ElgatoSession(cfg, icons, project_icons=ProjectIconStore())
    sess.set_slots([("s0", (0, 0)), ("s1", (1, 0))])
    sess.set_action_keys([("a0", "approve", (0, 1))])
    state = AgentState(AgentKey("dev", "p1"), "claude", "api", Status.BLOCKED)
    state.repo, state.title = "api", "approve rm"
    sess.apply_snapshot("dev", [state])
    return sess, icons


def test_render_all_stamps_every_key(mine_theme):
    sess, icons = _session()
    sess.render_all()
    assert icons.tiles and {t.deck_theme for t in icons.tiles} == {"mine"}


def test_unknown_theme_id_is_stamped_as_classic(mine_theme):
    sess, icons = _session("nope")
    sess.render_all()
    assert icons.tiles and {t.deck_theme for t in icons.tiles} == {"classic"}


def test_plugin_keys_carry_kind_status_and_title():
    sess, icons = _session()
    sess.render_all()
    agent = next(t for t in icons.tiles if t.kind == "agent")
    assert (agent.status, agent.title) == ("blocked", "approve rm")
    assert {t.kind for t in icons.tiles} == {"agent", "empty", "control"}
    action = next(t for t in icons.tiles if t.kind == "control")
    assert action.repo is not None  # routed to Theme.agent_tile, kind tells it apart


def test_offline_agent_status_on_the_plugin():
    sess, icons = _session()
    sess.set_connection("dev", False)
    sess.render_all()
    assert next(t for t in icons.tiles if t.kind == "agent").status == "offline"
