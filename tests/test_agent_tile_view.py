"""One agent-tile TileView factory for the deck and the Elgato plugin.

The Elgato session once built its own TileView copy and silently missed a new
field (tile_fill). Both paths now go through layout.agent_tile_view; this pins
that every field the orchestrator sets is set by the Elgato path too, except
the ones the plugin deliberately does not show (listed with the reason).
"""

from __future__ import annotations

import dataclasses

from herdeck import layout
from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig
from herdeck.driver.base import TileView
from herdeck.elgato.session import ElgatoSession
from herdeck.model import AgentKey, AgentState, Status
from herdeck.orchestrator import Orchestrator
from herdeck.project_icons import ProjectIconStore

# Fields only the D200/desktop deck fills in, and why the plugin leaves them unset.
ORCHESTRATOR_ONLY = {
    "spinner": "Elgato keys are static images pushed on change, never animated",
    "time_text": "the plugin tile has no elapsed-time column",
    "pinned": "the plugin has no pins",
    "server_tag": "the plugin shows no backend tags",
    "server_accent": "the plugin shows no backend tags",
    "section": "click-to-jump into the settings editor exists only in the desktop app",
}


class _NoIcons:
    def render_tile_bytes(self, tile):
        return b""


def _config() -> Config:
    cfg = Config(
        servers=[ServerConfig("dev", "ws://dev", "t")],
        profiles=dict(DEFAULT_PROFILES),
        overview_order=["dev"],
        grid=(5, 3),
    )
    # Every view setting that reaches the tile, off its default, so a path
    # that forgets one keeps the default and the comparison below sees it.
    cfg.view.working_animation = "pulse"
    cfg.view.tile_fill = "solid"
    cfg.view.tile_icon = "both"
    return cfg


def _state() -> AgentState:
    s = AgentState(AgentKey("dev", "p1"), "claude", "herdeck", Status.WORKING)
    s.repo, s.branch = "herdeck", "main"
    return s


def _both_tiles() -> tuple[TileView, TileView]:
    cfg = _config()
    orch = Orchestrator(cfg, slots=13, project_icons=ProjectIconStore())
    orch.apply_snapshot("dev", [_state()])
    deck = orch.render().tiles[0]

    sess = ElgatoSession(cfg, _NoIcons(), project_icons=ProjectIconStore())
    sess.set_slots([("s0", (0, 0))])
    sess.apply_snapshot("dev", [_state()])
    return deck, sess._slot_tile(0)


def _default(f: dataclasses.Field):
    if f.default is not dataclasses.MISSING:
        return f.default
    return dataclasses.MISSING


def test_elgato_sets_every_field_the_orchestrator_sets():
    deck, elgato = _both_tiles()
    assert deck.repo is not None and elgato.repo is not None  # both are agent tiles
    missing = []
    for f in dataclasses.fields(TileView):
        if f.name in ORCHESTRATOR_ONLY:
            continue
        default = _default(f)
        if getattr(deck, f.name) != default and getattr(elgato, f.name) == default:
            missing.append(f.name)
    assert not missing, (
        f"Elgato agent tile leaves {missing} at the default; build it through "
        "layout.agent_tile_view, or list the field in ORCHESTRATOR_ONLY with a reason"
    )


def test_config_driven_tile_fields_match_between_paths():
    deck, elgato = _both_tiles()
    for name in (
        "label",
        "agent_type",
        "working_animation",
        "tile_fill",
        "tile_icon",
        "project_icon",
        "project_name",
    ):
        assert getattr(elgato, name) == getattr(deck, name), name


def test_agent_tile_view_fills_the_view_settings():
    cfg = _config()
    tile = layout.agent_tile_view(
        4,
        _state(),
        cfg.view,
        color="green",
        repo="herdeck",
        branch="main",
        status_text="WORKING",
        project_icons=ProjectIconStore(),
    )
    assert (tile.index, tile.label, tile.color, tile.agent_type) == (4, "herdeck", "green", "claude")
    assert (tile.working_animation, tile.tile_fill, tile.tile_icon) == ("pulse", "solid", "both")
    assert tile.project_name == "herdeck"
    assert tile.spinner is None and tile.section is None and tile.pinned is False
