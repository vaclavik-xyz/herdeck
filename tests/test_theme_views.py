"""View data the deck themes read (driver/base.py): TileView/PanelView theme
fields, TileView.kind derivation, and PanelView.cache_key staying identical
for classic (the D200 names its panel file after it)."""

from __future__ import annotations

from herdeck.driver.base import (
    PANEL_KINDS,
    TILE_KINDS,
    PanelAgent,
    PanelView,
    ThemeEnv,
    TileView,
)


def test_tile_theme_fields_default_to_classic():
    tile = TileView(0, "Back", "grey")
    assert tile.deck_theme == "classic"
    assert tile.theme_env == ThemeEnv()
    assert (tile.status, tile.title, tile.workspace) == (None, "", "")


def test_tile_kind_is_derived_when_not_given():
    assert TileView(0, "x", "green", repo="herdeck").kind == "agent"
    assert TileView(0, "", "empty").kind == "empty"
    assert TileView(0, "+ New", "launcher").kind == "launcher"
    assert TileView(0, "Back", "grey").kind == "control"
    # a launcher menu entry carries an agent type but no repo: not an agent tile
    assert TileView(0, "claude", "blue", agent_type="claude").kind == "control"
    assert TileView(0, "1", "amber", subtext="Yes", kind="option").kind == "option"


def test_kind_vocabularies():
    assert set(TILE_KINDS) == {
        "agent", "option", "control", "launcher", "empty", "idle_group", "pinned_hole", "menu"
    }
    assert set(PANEL_KINDS) == {
        "calm", "spotlight", "offline", "config_error", "detail", "usage", "menu", "sent"
    }


def test_theme_env_is_hashable():
    env = ThemeEnv("cs", (("blocked", "amber"), ("working", "green")))
    assert hash(env) == hash(ThemeEnv("cs", (("blocked", "amber"), ("working", "green"))))


def test_classic_panel_cache_key_ignores_theme_fields():
    plain = PanelView("All clear", meta="3 agents")
    rich = PanelView(
        "All clear",
        meta="3 agents",
        kind="sent",
        queue=[PanelAgent("api", elapsed="4m")],
        counts={"blocked": 1, "offline": 2},
        theme_env=ThemeEnv("cs", (("idle", "blue"),)),
    )
    assert rich.cache_key() == plain.cache_key()


def test_non_classic_panel_cache_key_covers_theme_fields():
    def key(**kw):
        return PanelView("All clear", deck_theme="signal", **kw).cache_key()

    base = key()
    assert key(theme_rev="1-2") != base
    assert key(kind="spotlight") != base
    assert key(queue=[PanelAgent("api", elapsed="4m")]) != base
    assert key(queue=[PanelAgent("api", elapsed="5m")]) != key(queue=[PanelAgent("api", elapsed="4m")])
    assert key(counts={"offline": 1}) != base
    assert key(theme_env=ThemeEnv("cs")) != base
    assert PanelView("All clear", deck_theme="quiet").cache_key() != base
