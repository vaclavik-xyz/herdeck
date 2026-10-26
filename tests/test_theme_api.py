"""The public deck-theme API (herdeck.themes.api): Theme defaults and the
ThemeContext helpers a theme may use."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

import herdeck
from herdeck import icons
from herdeck.driver.base import COLORS, PanelAgent, ThemeEnv, TileView
from herdeck.i18n import tr
from herdeck.project_icons import ProjectIconStore
from herdeck.themes.api import API_VERSION, PANEL_H, TILE, Theme, ThemeContext


def _draw():
    return ImageDraw.Draw(Image.new("RGB", (TILE, TILE)))


def _provider(tmp_path, store=None):
    return icons.IconProvider(
        cache_dir=str(tmp_path / "cache"),
        slug_map={},
        fetch=lambda slug: None,
        project_icons=store if store is not None else ProjectIconStore(),
    )


def _png(color=(200, 40, 60, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (16, 16), color).save(buf, "PNG")
    return buf.getvalue()


def test_api_constants():
    assert API_VERSION == 1
    assert (TILE, PANEL_H) == (196, 196)
    assert ThemeContext.TILE == 196


def test_theme_base_class_draws_nothing_and_falls_back_to_classic():
    theme = Theme()
    ctx = ThemeContext(ThemeEnv())
    tile = TileView(0, "Back", "grey")
    assert theme.agent_tile(tile, ctx, None) is None
    assert theme.key_tile(tile, ctx) is None
    assert theme.panel(None, ctx, 458) is None
    assert (theme.api_version, theme.base, theme.animated) == (1, "classic", False)


def test_color_resolves_the_users_status_colors_first():
    ctx = ThemeContext(ThemeEnv("en", (("blocked", "pink"),)))
    assert ctx.color("blocked") == COLORS["pink"]  # [theme.colors] override
    assert ctx.color("amber") == COLORS["amber"]  # palette name
    assert ctx.color("no-such-colour") == COLORS["grey"]
    assert ctx.status_color("blocked") == COLORS["pink"]
    assert ctx.status_color(None) == COLORS["grey"]


def test_language_and_tr_follow_the_env():
    ctx = ThemeContext(ThemeEnv("cs"))
    assert ctx.language == "cs"
    assert ctx.tr("needs_you_one") == tr("cs", "needs_you_one")


def test_text_helpers():
    ctx = ThemeContext(ThemeEnv())
    d = _draw()
    font, text = ctx.fit_text(d, "herdeck", 180, 40, 20)
    assert text == "herdeck" and font.size <= 40
    font, text = ctx.fit_text(d, "an extremely long project name " * 3, 120, 30, 22)
    assert font.size == 22 and text.endswith("…")
    lines = ctx.wrap(d, "fix the flaky render golden on linux runners", ctx.font(24), 170, 2)
    assert 1 <= len(lines) <= 2
    assert ctx.ink_for(COLORS["amber"]) == (0, 0, 0)
    assert ctx.ink_for((20, 20, 20)) == (255, 255, 255)
    assert ctx.readable((50, 120, 220), (26, 26, 30)) != (50, 120, 220)  # lightened


def test_project_mark_without_a_provider_is_a_monogram():
    ctx = ThemeContext(ThemeEnv())
    mark = ctx.project_mark(PanelAgent("api", project_name="api"), 40)
    assert mark.size == (40, 40) and mark.mode == "RGBA"


def test_project_mark_uses_the_favicon_and_flags_missing_bytes(tmp_path):
    store = ProjectIconStore()
    store.put("h1", "image/png", _png())
    provider = _provider(tmp_path, store)
    ctx = ThemeContext(ThemeEnv(), provider, tile_render=True)
    fav = ctx.project_mark(TileView(0, "x", "green", project_icon="h1", project_name="api"), 64)
    mono = ctx.project_mark(TileView(0, "x", "green", project_name="api"), 64)
    assert fav.tobytes() != mono.tobytes()
    assert provider._render_uncacheable is False
    ctx.project_mark(TileView(0, "x", "green", project_icon="gone", project_name="api"), 64)
    assert provider._render_uncacheable is True  # filed under the monogram name


def test_agent_mark_sizes(tmp_path):
    with_provider = ThemeContext(ThemeEnv(), _provider(tmp_path))
    assert with_provider.agent_mark("claude", 46).size == (46, 46)
    assert with_provider.agent_mark(None, 30, dark=True).size == (30, 30)
    assert ThemeContext(ThemeEnv()).agent_mark("codex", 40).size == (40, 40)


def test_api_imports_first_without_a_cycle():
    # herdeck.icons imports the themes lazily; importing the API first (what a
    # user theme does) must not trip a circular import.
    src = str(Path(herdeck.__file__).resolve().parents[1])  # this checkout's src/
    out = subprocess.run(
        [sys.executable, "-c", "import herdeck.themes.api as a; print(a.API_VERSION)"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": src},
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "1"
