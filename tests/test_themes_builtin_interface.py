"""Interface check: the built-in themes (plan A2) are written against the
theme platform of plan A1. If any name below differs, A1 and A2 disagree —
align tests/theme_support.py (and, if needed, the A2 code) to A1 BEFORE
writing any theme code."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

from PIL import Image, ImageDraw
from theme_support import make_ctx, make_provider

from herdeck.driver.base import COLORS, PanelAgent, PanelView, ThemeEnv, TileView
from herdeck.themes import api, registry


def test_api_version_and_sizes():
    assert api.API_VERSION == 1
    assert (api.TILE, api.PANEL_H) == (196, 196)


def test_view_fields_exist_with_defaults():
    tile_fields = {f.name for f in dataclasses.fields(TileView)}
    assert {"deck_theme", "theme_env", "status", "kind", "title", "workspace"} <= tile_fields
    tile = TileView(0, "x", "grey")
    assert (tile.deck_theme, tile.kind) == ("classic", "control")
    assert TileView(0, "+ New", "launcher").kind == "launcher"
    assert TileView(0, "x", "green", repo="r").kind == "agent"
    panel_fields = {f.name for f in dataclasses.fields(PanelView)}
    assert {"deck_theme", "theme_env", "kind", "queue", "counts"} <= panel_fields
    panel = PanelView("x")
    assert (panel.deck_theme, panel.kind, panel.queue, panel.counts) == ("classic", "calm", [], {})
    assert [f.name for f in dataclasses.fields(PanelAgent)] == [
        "label", "title", "elapsed", "agent_type", "project_icon", "project_name",
    ]
    assert ThemeEnv("cs", (("working", "pink"),)).language == "cs"


def test_a_theme_subclass_delegates_what_it_does_not_draw(tmp_path):
    class Probe(api.Theme):
        id = "probe"
        name = {"en": "Probe", "cs": "Sonda"}
        base = "classic"

    ctx = make_ctx(make_provider(tmp_path))
    probe = Probe()
    assert (probe.api_version, probe.animated) == (1, False)
    assert probe.agent_tile(TileView(0, "x", "green", repo="r", status="working"), ctx, None) is None
    assert probe.key_tile(TileView(0, "Back", "grey"), ctx) is None
    assert probe.panel(PanelView("x"), ctx, 458) is None


def test_context_surface(tmp_path):
    ctx = make_ctx(make_provider(tmp_path), "cs", {"working": "pink"})
    draw = ImageDraw.Draw(Image.new("RGB", (196, 196)))
    assert ctx.TILE == 196
    assert ctx.language == "cs"
    assert ctx.font(22).size == 22 and ctx.font(22, bold=False).size == 22
    lines = ctx.wrap(draw, "one two three four five six seven", ctx.font(22), 80, 2)
    assert isinstance(lines, list) and 1 <= len(lines) <= 2
    assert all(isinstance(line, str) for line in lines)
    assert ctx.ink_for((255, 255, 255)) == (0, 0, 0)
    assert len(ctx.readable((50, 120, 220), (16, 17, 20))) == 3
    assert ctx.color("red") == COLORS["red"]  # a palette name resolves directly
    assert ctx.status_color("working") == COLORS["pink"]  # [theme.colors] respected
    assert ctx.status_color("offline") == COLORS["red"]
    # tr(key) without arguments returns the raw template (themes format it)
    assert ctx.tr("server_offline") == "{name} offline"
    sources = [
        TileView(0, "x", "grey", tile_icon="project", project_name="herdeck"),
        PanelAgent("lumen", "task", "4m", "claude", None, "lumen"),
        SimpleNamespace(project_icon=None, project_name="x", repo="x", label="x"),
    ]
    for source in sources:
        mark = ctx.project_mark(source, 40)
        assert mark.size == (40, 40) and mark.mode == "RGBA"
    for dark in (False, True):
        mark = ctx.agent_mark("claude", 40, dark=dark)
        assert mark.size == (40, 40) and mark.mode == "RGBA"


def test_registry_surface(tmp_path):
    assert registry.BUILTIN_MODULES["classic"] == "herdeck.themes.classic"
    reg = registry.ThemeRegistry(str(tmp_path))
    assert reg.resolve("classic").id == "classic"
    assert reg.resolve("no-such-theme").id == "classic"
    assert callable(registry.default_registry) and callable(registry.set_default_registry)
