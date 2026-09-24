from __future__ import annotations

import os
import sys
from collections.abc import Callable

from PIL import Image

from .icons import ICON_SIZE, _baked_glyph_png_name

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
    return _baked_glyph_png_name(svg_text)


def make_png_rasterizer(baked_dir: str) -> Callable[[str, int], Image.Image]:
    """Returns the pre-baked PNG for a bundled SVG glyph; any other SVG (a
    project favicon) is rendered by resvg, which ships in the frozen bundle."""

    def rasterize(svg: str, size: int) -> Image.Image:
        path = os.path.join(baked_dir, glyph_png_name(svg))
        if not os.path.exists(path):
            from .icons import resvg_rasterize

            return resvg_rasterize(svg, size)
        img = Image.open(path).convert("RGBA")
        if img.size != (size, size):
            img = img.resize((size, size))
        return img

    return rasterize


def prerasterize_assets(src_dir: str, out_dir: str, size: int = BAKE_SIZE) -> list[str]:
    """Build-time: rasterize each ``*.svg`` in ``src_dir`` to a content-keyed PNG.

    Uses resvg (the same rasterizer the runtime uses). Returns the
    baked PNG filenames (the bundle's glyph manifest). A PNG that already exists is
    left untouched — no re-encode, no mtime churn — so iterating only ``*.svg`` makes
    baking into the source assets dir (``out_dir == src_dir``) safe and idempotent.
    """
    from .icons import resvg_rasterize

    os.makedirs(out_dir, exist_ok=True)
    baked: list[str] = []
    for entry in sorted(os.listdir(src_dir)):
        if not entry.endswith(".svg"):
            continue
        with open(os.path.join(src_dir, entry), encoding="utf-8") as fh:
            svg = fh.read()
        name = glyph_png_name(svg)
        dst = os.path.join(out_dir, name)
        if not os.path.exists(dst):
            resvg_rasterize(svg, size).save(dst)
        baked.append(name)
    return baked
