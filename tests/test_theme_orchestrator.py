"""The Orchestrator hands deck themes their data: every render is stamped with
[view].deck_theme, tiles carry kind/status/title, overview panels carry
kind/counts/queue (the blocked queue, longest-waiting first)."""

from __future__ import annotations

from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig
from herdeck.model import AgentKey, AgentState, Status
from herdeck.orchestrator import Orchestrator
from herdeck.project_icons import ProjectIconStore
from herdeck.themes import registry as reg_mod
from herdeck.themes.registry import ThemeRegistry


def _config(theme: str = "classic") -> Config:
    cfg = Config(
        servers=[ServerConfig("dev", "ws://dev", "t")],
        profiles=dict(DEFAULT_PROFILES),
        overview_order=["dev"],
        grid=(5, 3),
    )
    cfg.view.deck_theme = theme
    return cfg


def _state(pane: str, status: Status, label: str, **kw) -> AgentState:
    s = AgentState(AgentKey("dev", pane), "claude", label, status)
    s.repo = label
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _orch(theme: str = "classic"):
    now = [1000.0]
    orch = Orchestrator(
        _config(theme), slots=13, project_icons=ProjectIconStore(), clock=lambda: now[0]
    )
    return orch, now


def _install_theme(tmp_path):
    (tmp_path / "mine.py").write_text(
        "from herdeck.themes.api import Theme\n\n\n"
        "class T(Theme):\n    id = 'mine'\n\n\nTHEME = T()\n",
        encoding="utf-8",
    )
    reg_mod.set_default_registry(ThemeRegistry(str(tmp_path)))


def _assert_stamped(rs):
    assert rs.tiles and {t.deck_theme for t in rs.tiles} == {"mine"}
    assert rs.panel.deck_theme == "mine"
    assert rs.panel.theme_rev.startswith("mine:")


def test_every_render_exit_is_stamped_with_the_deck_theme(tmp_path):
    _install_theme(tmp_path)
    try:
        orch, _ = _orch("mine")
        orch.apply_snapshot("dev", [_state("p1", Status.BLOCKED, "api")])
        rs = orch.render()
        _assert_stamped(rs)
        assert rs.tiles[0].theme_env.language == "en"
        assert ("blocked", "amber") in rs.panel.theme_env.colors
        orch.open_agent(AgentKey("dev", "p1"))
        orch.set_detection("Allow?\n1. Yes\n2. No")
        _assert_stamped(orch.render())
        orch.set_config_error(True, "dev")
        _assert_stamped(orch.render())
    finally:
        reg_mod.set_default_registry(None)


def test_launcher_render_is_stamped(tmp_path):
    _install_theme(tmp_path)
    try:
        orch, _ = _orch("mine")
        orch.on_press(12)
        _assert_stamped(orch.render())
    finally:
        reg_mod.set_default_registry(None)


def test_unknown_deck_theme_is_stamped_as_classic():
    orch, _ = _orch("nope")
    orch.apply_snapshot("dev", [_state("p1", Status.WORKING, "api")])
    rs = orch.render()
    assert {t.deck_theme for t in rs.tiles} == {"classic"}
    assert rs.panel.deck_theme == "classic"
    assert rs.panel.theme_rev == ""


def test_overview_tiles_carry_kind_status_and_title():
    orch, _ = _orch()
    orch.apply_snapshot(
        "dev", [_state("p1", Status.WORKING, "api", title="fix golden", workspace="ws")]
    )
    tiles = orch.render().tiles
    agent = tiles[0]
    assert (agent.kind, agent.status, agent.title, agent.workspace) == (
        "agent", "working", "fix golden", "ws"
    )
    assert tiles[1].kind == "empty"
    assert tiles[-1].kind == "launcher"  # launcher_menu: the reserved "+ New" tile


def test_spotlight_panel_carries_the_blocked_queue_longest_first():
    orch, now = _orch()
    orch.apply_snapshot("dev", [_state("p1", Status.BLOCKED, "old", title="approve rm")])
    now[0] += 60
    orch.apply_event("dev", _state("p2", Status.BLOCKED, "new"))
    now[0] += 30
    panel = orch.render().panel
    assert panel.kind == "spotlight"
    assert [(a.label, a.title, a.elapsed) for a in panel.queue] == [
        ("old", "approve rm", orch._elapsed_text(AgentKey("dev", "p1"))),
        ("new", "", orch._elapsed_text(AgentKey("dev", "p2"))),
    ]
    assert panel.counts["blocked"] == 2


def test_offline_panel_counts_offline_agents():
    orch, _ = _orch()
    orch.apply_snapshot("dev", [_state("p1", Status.WORKING, "api")])
    orch.set_connection("dev", False)
    rs = orch.render()
    assert rs.panel.kind == "offline"
    assert rs.panel.counts["offline"] == 1
    assert rs.tiles[0].status == "offline"


def test_calm_panel_kind_and_counts():
    orch, _ = _orch()
    orch.apply_snapshot("dev", [_state("p1", Status.IDLE, "api")])
    panel = orch.render().panel
    assert panel.kind == "calm"
    assert panel.counts == {
        "blocked": 0, "working": 0, "idle": 1, "done": 0, "waiting": 0, "offline": 0
    }
    assert panel.queue == []


def test_drill_launcher_and_config_error_kinds():
    orch, _ = _orch()
    key = AgentKey("dev", "p1")
    orch.apply_snapshot("dev", [_state("p1", Status.BLOCKED, "api")])
    orch.open_agent(key)
    orch.set_detection("Allow?\n1. Yes\n2. No")
    rs = orch.render()
    assert rs.panel.kind == "detail"
    assert "option" in {t.kind for t in rs.tiles}
    assert rs.tiles[-1].kind == "control"  # Back
    orch.set_config_error(True, "dev")
    assert orch.render().panel.kind == "config_error"


def test_launcher_menu_kinds():
    orch, _ = _orch()
    orch.on_press(12)  # the reserved "+ New" tile opens the launcher
    rs = orch.render()
    assert rs.panel.kind == "menu"
    assert rs.tiles[0].kind == "menu"  # a start profile entry
    assert rs.tiles[-1].kind == "control"  # Back
