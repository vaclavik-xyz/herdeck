"""Draw a tile or the panel through its deck theme's chain (theme -> its
bases -> classic): the first part that returns an image wins.

A part that raises is drawn by the next theme in the chain (logged once per
theme + part); an image of the wrong size is resized (logged once). classic is
the last resort and is never second-guessed: its errors propagate exactly as
before themes existed.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from PIL import Image

from ..driver.base import PanelView, ThemeEnv, TileView
from .api import PANEL_H, TILE, Theme, ThemeContext
from .registry import default_registry

log = logging.getLogger(__name__)
_warned: set[tuple[str, str]] = set()
_warned_lock = threading.Lock()


def _warn_once(theme_id: str, what: str, msg: str, *args, exc_info: bool = False) -> None:
    with _warned_lock:
        if (theme_id, what) in _warned:
            return
        _warned.add((theme_id, what))
    log.warning(msg, *args, exc_info=exc_info)


def _run(
    part: str,
    chain: tuple[Theme, ...],
    draw: Callable[[Theme], Image.Image | None],
    size: tuple[int, int],
    mode: str,
) -> Image.Image:
    for theme in chain:
        if theme.id == "classic":
            img = draw(theme)  # the reference look: errors propagate as before
        else:
            try:
                img = draw(theme)
            except Exception:
                _warn_once(
                    theme.id,
                    part,
                    "deck theme %s raised while drawing %s; its base draws it",
                    theme.id,
                    part,
                    exc_info=True,
                )
                continue
            if img is None:
                continue
            if not isinstance(img, Image.Image):
                _warn_once(theme.id, part + ":type", "deck theme %s returned a non-image for %s", theme.id, part)
                continue
        if img.size != size:
            _warn_once(
                theme.id,
                part + ":size",
                "deck theme %s drew %s at %dx%d, expected %dx%d; resizing",
                theme.id,
                part,
                *img.size,
                *size,
            )
            img = img.resize(size, Image.LANCZOS)
        return img if img.mode == mode else img.convert(mode)
    raise RuntimeError(f"no deck theme drew {part}")  # unreachable: classic always draws


def draw_tile(tile: TileView, frame: int | None, icons) -> Image.Image:
    """A key image (RGBA, TILE x TILE). ``agent_tile`` for tiles with ``repo``
    set, ``key_tile`` for the rest — the same split classic always made."""
    # getattr: tests (and old callers) hand in duck-typed tiles
    chain = default_registry().chain(getattr(tile, "deck_theme", "classic"))
    ctx = ThemeContext(getattr(tile, "theme_env", None) or ThemeEnv(), icons, tile_render=True)
    if tile.repo is not None:
        return _run("agent_tile", chain, lambda t: t.agent_tile(tile, ctx, frame), (TILE, TILE), "RGBA")
    return _run("key_tile", chain, lambda t: t.key_tile(tile, ctx), (TILE, TILE), "RGBA")


def draw_panel(panel: PanelView, width: int, icons=None) -> Image.Image:
    """The status window image (RGB, width x PANEL_H)."""
    chain = default_registry().chain(getattr(panel, "deck_theme", "classic"))
    ctx = ThemeContext(getattr(panel, "theme_env", None) or ThemeEnv(), icons)
    return _run("panel", chain, lambda t: t.panel(panel, ctx, width), (width, PANEL_H), "RGB")
