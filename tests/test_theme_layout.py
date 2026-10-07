"""Theme data built by the shared layout helpers (layout.py): agent tile
status/kind/title/workspace, overview panel kind/counts/queue, the blocked
queue entries and the render-exit stamp."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from herdeck import layout
from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig, ViewConfig
from herdeck.driver.base import PanelAgent, PanelView, ThemeEnv, TileView
from herdeck.model import AgentKey, AgentState, Status
from herdeck.project_icons import ProjectIconStore, tile_icon_fields
from herdeck.themes import registry as reg_mod
from herdeck.themes.registry import ThemeRegistry


def _state(status=Status.WORKING, server="dev", pane="p1", **kw) -> AgentState:
    s = AgentState(AgentKey(server, pane), "claude", "herdeck", status)
    s.repo, s.branch = "herdeck", "main"
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (16, 16), (10, 200, 10, 255)).save(buf, "PNG")
    return buf.getvalue()


def _tile(view=None, state=None, **kw) -> TileView:
    return layout.agent_tile_view(
        0,
        state or _state(),
        view or ViewConfig(),
        color="green",
        repo="herdeck",
        branch="main",
        status_text="WORKING",
        **kw,
    )


def test_agent_tile_carries_status_kind_title_and_workspace():
    tile = _tile(state=_state(title="fix the golden", workspace="herdeck-ws"))
    assert (tile.status, tile.kind) == ("working", "agent")
    assert (tile.title, tile.workspace) == ("fix the golden", "herdeck-ws")
    assert _tile(down=True).status == "offline"
    assert _tile(state=_state(Status.BLOCKED)).status == "blocked"


def test_project_mark_resolves_in_agent_mode_only_for_a_non_classic_theme():
    store = ProjectIconStore()
    store.put("h1", "image/png", _png())
    state = _state(project_icon="h1")
    classic = _tile(state=state, project_icons=store)
    assert (classic.tile_icon, classic.project_icon, classic.project_name) == ("agent", None, "")
    themed_view = ViewConfig(deck_theme="signal")
    themed = _tile(view=themed_view, state=state, project_icons=store)
    assert (themed.tile_icon, themed.project_icon, themed.project_name) == ("agent", "h1", "herdeck")


def test_tile_icon_fields_resolve_always():
    store = ProjectIconStore()
    store.put("h1", "image/png", _png())
    fields = tile_icon_fields(ViewConfig(), _state(project_icon="h1"), store, resolve_always=True)
    assert fields == {"tile_icon": "agent", "project_icon": "h1", "project_name": "herdeck"}


def test_summary_counts_offline_agents():
    agents = [
        _state(Status.WORKING, server="a", pane="1"),
        _state(Status.BLOCKED, server="b", pane="2"),
        _state(Status.IDLE, server="b", pane="3", lifecycle="closed"),
    ]
    counts = layout.summary(agents, down={"b"})
    assert (counts.working, counts.blocked, counts.offline) == (1, 1, 1)
    assert layout.summary(agents).offline == 0


@pytest.mark.parametrize(
    ("kwargs", "kind"),
    [
        ({}, "calm"),
        ({"sent": "Sent to api"}, "sent"),
        ({"spotlight": ("api", "4m")}, "spotlight"),
        ({"down": {"m4"}}, "offline"),
    ],
)
def test_overview_panel_kind_counts_and_queue(kwargs, kind):
    queue = [PanelAgent("api", elapsed="4m")]
    args = dict(spotlight=None, down=set())
    args.update(kwargs)
    panel = layout.panel_overview(
        layout.Counts(1, 2, 3, 0, offline=1),
        0,
        1,
        args.pop("down"),
        6,
        args.pop("spotlight"),
        queue=queue,
        **args,
    )
    assert panel.kind == kind
    assert panel.counts == {
        "blocked": 1, "working": 2, "idle": 3, "done": 0, "waiting": 0, "offline": 1
    }
    assert panel.queue == queue


def test_panel_agent_entry():
    store = ProjectIconStore()
    store.put("h1", "image/png", _png())
    state = _state(Status.BLOCKED, title="approve rm", project_icon="h1")
    entry = layout.panel_agent(state, "4m", ViewConfig(), store)
    assert entry == PanelAgent("herdeck", "approve rm", "4m", "claude", "h1", "herdeck")


def _config(theme="classic") -> Config:
    cfg = Config(
        servers=[ServerConfig("dev", "ws://dev", "t")],
        profiles=dict(DEFAULT_PROFILES),
        overview_order=["dev"],
        grid=(5, 3),
    )
    cfg.view.deck_theme = theme
    cfg.view.language = "cs"
    cfg.theme.colors["blocked"] = "pink"
    return cfg


def test_stamp_theme_sets_theme_and_env():
    tiles = [TileView(0, "Back", "grey")]
    panel = PanelView("x")
    layout.stamp_theme(_config(), tiles, panel)
    env = tiles[0].theme_env
    assert isinstance(env, ThemeEnv) and env.language == "cs"
    assert ("blocked", "pink") in env.colors
    assert panel.theme_env == env and panel.deck_theme == "classic"
    assert panel.theme_rev == ""  # classic: no registry lookup


def test_stamp_theme_records_a_user_theme_fingerprint(tmp_path):
    (tmp_path / "mine.py").write_text(
        "from herdeck.themes.api import Theme\n\n\n"
        "class T(Theme):\n    id = 'mine'\n\n\nTHEME = T()\n",
        encoding="utf-8",
    )
    reg_mod.set_default_registry(ThemeRegistry(str(tmp_path)))
    try:
        tiles, panel = [TileView(0, "Back", "grey")], PanelView("x")
        layout.stamp_theme(_config("mine"), tiles, panel)
        assert tiles[0].deck_theme == panel.deck_theme == "mine"
        assert panel.theme_rev.startswith("mine:")
    finally:
        reg_mod.set_default_registry(None)


def test_stamp_theme_stamps_the_resolved_id_for_an_unknown_theme():
    tiles, panel = [TileView(0, "Back", "grey")], PanelView("x")
    layout.stamp_theme(_config("nope"), tiles, panel)
    assert tiles[0].deck_theme == panel.deck_theme == "classic"
    assert panel.theme_rev == ""
