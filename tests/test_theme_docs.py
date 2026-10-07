"""docs/themes.md is the theme author's guide: it must cover the whole public
API and quote the example theme the registry tests load."""

from __future__ import annotations

from pathlib import Path

import pytest

from herdeck.driver.base import PANEL_KINDS, TILE_KINDS

ROOT = Path(__file__).resolve().parents[1]
GUIDE = (ROOT / "docs" / "themes.md").read_text(encoding="utf-8")
EXAMPLE = (ROOT / "docs" / "themes" / "minimal.py").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "name",
    [
        "API_VERSION", "Theme", "ThemeContext", "PanelAgent", "agent_tile(", "key_tile(",
        "panel(", "api_version", "animated", "base", "ctx.TILE", "ctx.language", "ctx.tr(",
        "ctx.font(", "ctx.fit_text(", "ctx.wrap(", "ctx.ink_for(", "ctx.readable(",
        "ctx.color(", "ctx.status_color(", "ctx.project_mark(", "ctx.agent_mark(",
    ],
)
def test_guide_documents_every_public_name(name):
    assert name in GUIDE


@pytest.mark.parametrize("kind", TILE_KINDS + PANEL_KINDS)
def test_guide_documents_every_kind(kind):
    assert f"`{kind}`" in GUIDE


def test_guide_quotes_the_example_theme_verbatim():
    start = EXAMPLE.index("class Minimal(Theme):")
    assert EXAMPLE[start:].strip() in GUIDE


def test_guide_warns_that_a_theme_is_code():
    assert "full user privileges" in GUIDE
