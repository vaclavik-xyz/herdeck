from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Callable

from PIL import Image

from ..icons import ICON_SIZE

BAKE_SIZE = ICON_SIZE


def is_frozen() -> bool:
    """True when running inside a PyInstaller (or similar) frozen bundle."""
    return bool(getattr(sys, "frozen", False))


def baked_assets_dir() -> str:
    """The bundled assets dir at runtime.

    PyInstaller sets ``sys._MEIPASS`` in both onefile and onedir modes; the
    ``.spec`` bundles ``src/herdeck/assets`` as data under ``herdeck_assets``.
    """
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(sys.executable))
    return os.path.join(base, "herdeck_assets")


def glyph_png_name(svg_text: str) -> str:
    """Content-addressed PNG filename for an SVG glyph.

    The build-time baker and the runtime loader both key on this, so neither
    needs to know the agent type — keeping ``IconProvider``'s ``rasterize(svg,
    size)`` seam untouched.
    """
    return hashlib.sha1(svg_text.encode("utf-8")).hexdigest() + ".png"


def make_png_rasterizer(baked_dir: str) -> Callable[[str, int], Image.Image]:
    """A Pillow-only rasterizer that returns a pre-baked PNG for an SVG glyph."""

    def rasterize(svg: str, size: int) -> Image.Image:
        path = os.path.join(baked_dir, glyph_png_name(svg))
        img = Image.open(path).convert("RGBA")
        if img.size != (size, size):
            img = img.resize((size, size))
        return img

    return rasterize
