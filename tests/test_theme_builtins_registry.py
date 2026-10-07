"""The three new built-ins are registered and reached through the platform's
real dispatch (IconProvider tiles, icons.compose_panel), including the base
fallback: headline -> classic, quiet -> signal. Every view carries the same
ThemeEnv as the direct render's context (as layout.stamp_theme does)."""

from __future__ import annotations

import dataclasses
import io

import pytest
from PIL import Image
from theme_support import agent, env, key, make_ctx, make_provider, panel_variants

from herdeck import icons
from herdeck.themes import api, headline, quiet, registry, signal

NEW = {"signal": signal.THEME, "headline": headline.THEME, "quiet": quiet.THEME}


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path):
    """Dispatch through a registry that sees no user themes."""
    registry.set_default_registry(registry.ThemeRegistry(str(tmp_path / "themes")))
    yield
    registry.set_default_registry(None)


@pytest.fixture
def provider(tmp_path):
    return make_provider(tmp_path)


def tile_png(provider, tile) -> bytes:
    return Image.open(io.BytesIO(provider.render_tile_bytes(tile))).convert("RGB").tobytes()


def test_builtins_are_registered_in_order(tmp_path):
    assert list(registry.BUILTIN_MODULES)[:4] == ["classic", "signal", "headline", "quiet"]
    for theme_id in NEW:
        assert registry.BUILTIN_MODULES[theme_id] == f"herdeck.themes.{theme_id}"
    reg = registry.ThemeRegistry(str(tmp_path / "themes"))
    listed = {info.id: info for info in reg.list()}
    for theme_id, theme in NEW.items():
        assert reg.resolve(theme_id) is theme
        assert theme.api_version == api.API_VERSION and theme.animated is False
        info = listed[theme_id]
        assert info.builtin and info.error is None and set(info.name) == {"en", "cs"}


def test_bases_point_at_builtins():
    assert signal.THEME.base == "classic"
    assert headline.THEME.base == "classic"
    assert quiet.THEME.base == "signal"


@pytest.mark.parametrize("status", ["blocked", "working", "idle", "done", "waiting", "offline"])
def test_agent_tiles_dispatch_to_the_theme(provider, status):
    ctx = make_ctx(provider)
    for theme in NEW.values():
        tile = agent(status, deck_theme=theme.id, theme_env=env())
        direct = theme.agent_tile(tile, ctx, None).convert("RGB").tobytes()
        assert tile_png(provider, tile) == direct, (theme.id, status)


def test_key_tiles_fall_back_along_the_base_chain(provider):
    ctx = make_ctx(provider)
    for args, extra in ((("Back", "grey", "control"), {}), (("1", "green", "option"), {"subtext": "Yes"})):
        signal_direct = signal.THEME.key_tile(key(*args, **extra), ctx).convert("RGB").tobytes()
        assert tile_png(provider, key(*args, theme_env=env(), deck_theme="signal", **extra)) == signal_direct
        # quiet draws no key tiles: its base, signal, does
        assert tile_png(provider, key(*args, theme_env=env(), deck_theme="quiet", **extra)) == signal_direct
        # headline draws no key tiles: its base, classic, does
        assert tile_png(provider, key(*args, theme_env=env(), deck_theme="headline", **extra)) == tile_png(
            provider, key(*args, theme_env=env(), deck_theme="classic", **extra)
        )


@pytest.mark.parametrize("width", [458, 392])
def test_panels_dispatch_and_fall_back(provider, width):
    ctx = make_ctx(provider)
    for name in ("spotlight_queue_note", "calm_usage_paged", "detail_blocked_short", "offline"):
        panel = panel_variants()[name]
        direct = signal.THEME.panel(panel, ctx, width).convert("RGB").tobytes()

        def via(theme_id, panel=panel):
            themed = dataclasses.replace(panel, deck_theme=theme_id, theme_env=env())
            return icons.compose_panel(themed, width, icons=provider).convert("RGB").tobytes()

        assert via("signal") == direct, name
        assert via("quiet") == direct, name
        assert via("headline") == via("classic"), name
