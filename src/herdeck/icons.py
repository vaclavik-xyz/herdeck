from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable

from PIL import Image, ImageDraw

from .driver.base import COLORS

ICON_SIZE = 196
# The spinner has this many distinct frames (arc steps 360/45); keep the cache
# bounded so a long-running working tile reuses frames instead of writing forever.
SPINNER_FRAMES = 8

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")


def _safe_name(agent_type: str) -> str:
    """Filesystem-safe, collision-resistant token for an inbound agent type.

    Known agent types (alphanumerics) pass through unchanged for readable
    filenames; anything else is sanitized and disambiguated with a short hash so
    distinct raw values (e.g. ``a/b`` vs ``a_b``) never share a cache file.
    """
    safe = _UNSAFE_NAME.sub("_", agent_type)
    if safe == agent_type and safe:
        return safe
    digest = hashlib.sha1(agent_type.encode()).hexdigest()[:8]
    return f"{safe or '_'}_{digest}"

# agent type -> Simple Icons slug (None => generated glyph fallback)
DEFAULT_AGENT_SLUGS: dict[str, str | None] = {
    "claude": "claude",
    "codex": None,            # no Simple Icons entry -> glyph
    "cursor": "cursor",
    "copilot": "githubcopilot",
    "gemini": "googlegemini",
    "opencode": "opencode",
    "default": None,
}


def _default_fetch(slug: str) -> str | None:
    """Fetch a Simple Icons SVG (cached on disk by the caller). Network."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"https://cdn.simpleicons.org/{slug}", timeout=5) as r:
            if r.status == 200:
                return r.read().decode()
    except Exception:
        return None
    return None


def _default_rasterize(svg: str, size: int) -> Image.Image:
    import cairosvg  # build-time only; not needed in tests
    png = cairosvg.svg2png(bytestring=svg.encode(), output_width=size, output_height=size)
    import io
    return Image.open(io.BytesIO(png)).convert("RGBA")


class IconProvider:
    """Resolves agent type -> composited tile-icon PNG in the strmdck icon dir.

    Precedence: user override PNG > Simple Icons (fetch+rasterize) > generated glyph.
    Output PNGs land in cache_dir (which must be the strmdck
    .cache/icons/_generated dir) and are referenced by bare filename.
    """

    def __init__(self, cache_dir: str, slug_map: dict[str, str | None],
                 overrides_dir: str | None = None,
                 fetch: Callable[[str], str | None] = _default_fetch,
                 rasterize: Callable[[str, int], Image.Image] = _default_rasterize):
        self._cache_dir = cache_dir
        self._slug_map = slug_map
        self._overrides_dir = overrides_dir
        self._fetch = fetch
        self._rasterize = rasterize
        os.makedirs(cache_dir, exist_ok=True)
        self._glyph_cache: dict[str, Image.Image] = {}

    def _base_glyph(self, agent_type: str) -> Image.Image:
        """A monochrome mark for an agent type: override PNG, Simple Icon, or letter."""
        if agent_type in self._glyph_cache:
            return self._glyph_cache[agent_type]
        img: Image.Image | None = None
        if self._overrides_dir:
            ov = os.path.join(self._overrides_dir, f"{_safe_name(agent_type)}.png")
            if os.path.exists(ov):
                img = Image.open(ov).convert("RGBA").resize((ICON_SIZE, ICON_SIZE))
        if img is None:
            slug = self._slug_map.get(agent_type)
            if slug:
                svg = self._fetch(slug)
                if svg:
                    img = self._rasterize(svg, ICON_SIZE)
        if img is None:
            img = self._letter_glyph(agent_type)
        self._glyph_cache[agent_type] = img
        return img

    def _letter_glyph(self, agent_type: str) -> Image.Image:
        img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        ch = (agent_type[:1] or "?").upper()
        # Center manually — the default bitmap font does not support anchor="mm".
        bbox = d.textbbox((0, 0), ch)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        d.text(((ICON_SIZE - w) // 2 - bbox[0], (ICON_SIZE - h) // 2 - bbox[1]),
               ch, fill=(255, 255, 255, 255))
        return img

    def icon_for(self, agent_type: str, color: str, spinner: int | None = None) -> str:
        """Return a cached PNG filename (in cache_dir) of the mark on a status bg."""
        if spinner is not None:
            spinner %= SPINNER_FRAMES   # bound the cache to a fixed frame set
        key = f"{_safe_name(agent_type)}_{color}" + (
            f"_s{spinner}" if spinner is not None else "")
        name = f"icon_{key}.png"
        path = os.path.join(self._cache_dir, name)
        if os.path.exists(path):
            return name
        bg = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), COLORS.get(color, COLORS["dim"]) + (255,))
        glyph = self._base_glyph(agent_type)
        bg.alpha_composite(glyph)
        if spinner is not None:
            self._draw_spinner(bg, spinner)
        bg.convert("RGB").save(path)
        return name

    def _draw_spinner(self, img: Image.Image, phase: int) -> None:
        d = ImageDraw.Draw(img)
        start = (phase * 45) % 360
        d.arc([10, 10, ICON_SIZE - 10, ICON_SIZE - 10], start, start + 90,
              fill=(255, 255, 255, 255), width=8)
