"""Back-compat re-export shim.

The frozen-render helpers are generic (they depend only on ``herdeck.icons``),
so they now live in ``herdeck.frozen``. This module re-exports them unchanged so
``herdeck.elgato.runtime`` and any existing imports/tests keep working.
"""

from __future__ import annotations

from ..frozen import (
    BAKE_SIZE,
    baked_assets_dir,
    glyph_png_name,
    is_frozen,
    make_png_rasterizer,
    prerasterize_assets,
)

__all__ = [
    "BAKE_SIZE",
    "baked_assets_dir",
    "glyph_png_name",
    "is_frozen",
    "make_png_rasterizer",
    "prerasterize_assets",
]
