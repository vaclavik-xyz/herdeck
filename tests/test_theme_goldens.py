"""Pixel goldens for the built-in themes (signal in full, headline and quiet
agent tiles). Same rules as tests/test_render_golden.py: vendored Inter font
and pre-baked agent marks make the digests OS-independent; a digest is a
SHA-256 of the raw RGB pixels (shared harness: tests/golden_support.py).

Regenerate after an intended change:
    HERDECK_UPDATE_GOLDENS=1 .venv/bin/pytest tests/test_theme_goldens.py
"""

from __future__ import annotations

import os

from golden_support import UPDATE_ENV, check_goldens
from PIL import Image
from theme_support import agent_variants, key_variants, make_ctx, make_provider, panel_variants

from herdeck.themes import headline, quiet, signal

GOLDEN_FILE = os.path.join(os.path.dirname(__file__), "goldens", "themes.json")

AGENT_GOLDENS = [
    "blocked",
    "working",
    "idle",
    "done",
    "waiting",
    "offline",
    "unknown",
    "working_project_subagents",
    "blocked_project_subagents",
    "long_title",
]


def _render_all(tmp_path) -> dict[str, Image.Image]:
    ctx = make_ctx(make_provider(tmp_path))
    agents = agent_variants()
    out: dict[str, Image.Image] = {}
    for theme in (signal.THEME, headline.THEME, quiet.THEME):
        for name in AGENT_GOLDENS:
            out[f"{theme.id}_agent_{name}"] = theme.agent_tile(agents[name], ctx, None)
    for name, tile in key_variants().items():
        out[f"signal_key_{name}"] = signal.THEME.key_tile(tile, ctx)
    for name, panel in panel_variants().items():
        for width in (458, 392):
            out[f"signal_panel_{name}_{width}"] = signal.THEME.panel(panel, ctx, width)
    return out


def test_builtin_themes_match_goldens(tmp_path):
    check_goldens(
        _render_all(tmp_path),
        GOLDEN_FILE,
        tmp_path,
        title="Theme pixels changed",
        note="If intended, look at the PNGs above, then regenerate with",
        regenerate=f"{UPDATE_ENV}=1 .venv/bin/pytest tests/test_theme_goldens.py",
        after="and commit tests/goldens/themes.json.",
    )
