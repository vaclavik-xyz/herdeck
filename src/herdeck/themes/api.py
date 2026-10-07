"""The public deck-theme API (``API_VERSION = 1``) — see docs/themes.md.

A theme decides how a key and the status window are drawn; everything else
(the Orchestrator, TileView/PanelView, caches, favicons, glyphs, the animation
clock, the drivers) is shared. A theme overrides any of three parts and returns
``None`` (or does not override) to let its ``base`` draw that part; the chain
always ends in ``classic``.

Only the names in ``__all__`` are the stable surface. Theme code must not reach
into ``herdeck.icons`` privates; ``ThemeContext`` exposes what it may use.
Themes must be stateless: parts can run concurrently on several render threads.
"""

from __future__ import annotations

from typing import Any

from PIL import Image, ImageDraw

from .. import icons as _icons
from ..driver.base import COLORS, PanelAgent, PanelView, ThemeEnv, TileView
from ..i18n import tr as _tr

__all__ = [
    "API_VERSION",
    "TILE",
    "PANEL_H",
    "PanelAgent",
    "PanelView",
    "Theme",
    "ThemeContext",
    "TileView",
]

API_VERSION = 1
TILE = _icons.ICON_SIZE  # every key image is TILE x TILE (196)
PANEL_H = _icons.PANEL_H  # the status window is width x PANEL_H (196)


class Theme:
    """Base class of every deck theme. Subclass it, set ``id``/``name``/``base``
    and override the parts you draw; expose an instance as module-level
    ``THEME``.

    ``agent_tile`` is called for every tile with ``repo`` set (agent tiles, and
    the Elgato plugin's action keys: ``tile.kind == "control"`` with the
    target's name in ``repo``); ``key_tile`` for every other key. ``frame`` is
    the animation phase of a working tile, always ``None`` unless the theme
    sets ``animated = True``. Images: RGB(A), exactly TILE x TILE for tiles,
    ``width`` x PANEL_H for the panel (anything else is resized, logged once).
    """

    api_version: int = API_VERSION
    id: str = ""
    name: dict[str, str] = {}  # {"en": ..., "cs": ...}; read-only
    base: str | None = "classic"
    # False: the platform never passes a frame and keeps ONE cache entry per
    # working tile (on the D200 every distinct frame is a full page reload).
    animated: bool = False

    def agent_tile(
        self, tile: TileView, ctx: ThemeContext, frame: int | None
    ) -> Image.Image | None:
        return None

    def key_tile(self, tile: TileView, ctx: ThemeContext) -> Image.Image | None:
        return None

    def panel(self, panel: PanelView, ctx: ThemeContext, width: int) -> Image.Image | None:
        return None


class ThemeContext:
    """The helpers a theme may use while drawing one part.

    Built per call by the platform (themes/dispatch.py); cheap and stateless
    from the theme's point of view."""

    TILE = TILE
    PANEL_H = PANEL_H

    def __init__(self, env: ThemeEnv, icons: Any = None, *, tile_render: bool = False):
        self._env = env
        self._icons = icons  # the IconProvider (or None, e.g. a panel-only surface)
        # A tile render notes a monogram drawn for a favicon whose bytes went
        # missing, so the tile is not cached under the favicon's name.
        self._tile_render = tile_render
        self._colors = dict(env.colors)

    @property
    def language(self) -> str:
        return self._env.language

    def tr(self, key: str, **fmt: object) -> str:
        """A rendered-deck string (herdeck.i18n STRINGS) in the deck language."""
        return _tr(self._env.language, key, **fmt)

    # --- text ---
    def font(self, size: int, bold: bool = True):
        """The vendored Inter (Bold or Regular) at ``size`` px, cached."""
        return _icons._font(size, bold=bold)

    def fit_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        max_w: float,
        largest: int,
        smallest: int,
        bold: bool = True,
    ):
        """``(font, text)``: the largest font in [smallest, largest] that fits
        ``text`` into ``max_w``; when even ``smallest`` does not fit, the text
        comes back cut with an ellipsis."""
        font = _icons._fit_font(draw, text, max_w, largest, smallest, bold=bold)
        return font, _icons._truncate(draw, text, font, max_w)

    def wrap(
        self, draw: ImageDraw.ImageDraw, text: str, font, max_w: float, max_lines: int = 2
    ) -> list[str]:
        """``text`` wrapped to <= ``max_lines`` lines no wider than ``max_w``
        (breaks at spaces and after '/'; a cut-off tail ends in an ellipsis)."""
        return _icons._wrap(draw, text, font, max_w, max_lines)

    # --- colour ---
    def ink_for(self, rgb) -> tuple[int, int, int]:
        """Black or white, whichever contrasts more with ``rgb`` (WCAG)."""
        return _icons._ink_for(tuple(rgb))

    def readable(self, rgb, bg) -> tuple[int, int, int]:
        """``rgb`` lightened just enough for 4.5:1 contrast on a dark ``bg``."""
        return _icons._readable(tuple(rgb), tuple(bg))

    def color(self, name: str) -> tuple[int, int, int]:
        """RGB of a colour name. A ``[theme.colors]`` key (working, idle,
        blocked, done, waiting, unknown, offline) resolves through the user's
        map first; palette names (green, amber, ...) resolve directly; anything
        unknown is grey."""
        name = self._colors.get(name, name)
        return COLORS.get(name, COLORS["grey"])

    def status_color(self, status: str | None) -> tuple[int, int, int]:
        """RGB of a TileView.status value ("working", ..., "offline")."""
        return self.color(status or "unknown")

    # --- marks ---
    def project_mark(self, item, size: int) -> Image.Image:
        """RGBA ``size`` x ``size`` project mark of a TileView or PanelAgent:
        its favicon (``project_icon``), else a coloured monogram of
        ``project_name``. No contrast plate — the theme decides the backdrop."""
        icon_hash = getattr(item, "project_icon", None) or ""
        name = getattr(item, "project_name", "") or ""
        base_of = getattr(self._icons, "_project_base", None)
        if base_of is not None:
            img, cacheable = base_of(icon_hash, name)
            if not cacheable and self._tile_render:
                self._icons._render_uncacheable = True
        else:
            from ..project_icons import default_store

            stored = default_store().get(icon_hash) if icon_hash else None
            img = _icons.project_mark_image(stored, name)
        return img.resize((size, size), Image.LANCZOS)

    def agent_mark(self, agent_type: str | None, size: int, dark: bool = False) -> Image.Image:
        """RGBA ``size`` x ``size`` agent logo (user override > bundled mark >
        letter); ``dark`` recolours a light mark black for bright backgrounds."""
        agent = agent_type or "default"
        logo = getattr(self._icons, "_logo", None)
        if logo is not None:
            return logo(agent, dark, size)
        img = _icons._monogram_image(agent)
        return img.resize((size, size), Image.LANCZOS)
