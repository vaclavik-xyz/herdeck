"""Every surface composes the panel with its IconProvider, so a deck theme can
draw favicons and agent marks on the status window."""

from __future__ import annotations

import io

from PIL import Image

from herdeck import icons as icons_mod
from herdeck.deckapp import DeckApp, MockSource
from herdeck.driver.base import PanelView
from herdeck.driver.web import WebDeck


class _StubIcons:
    def render_tile_bytes(self, tile):
        buf = io.BytesIO()
        Image.new("RGB", (4, 4)).save(buf, "PNG")
        return buf.getvalue()


def _capture(monkeypatch) -> list:
    seen: list = []
    real = icons_mod.compose_panel

    def spy(panel, *args, **kwargs):
        seen.append(kwargs.get("icons"))
        return real(panel, *args, **kwargs)

    monkeypatch.setattr(icons_mod, "compose_panel", spy)
    return seen


def test_web_simulator_passes_its_icon_provider(monkeypatch):
    seen = _capture(monkeypatch)
    stub = _StubIcons()
    deck = WebDeck(slots=13, serve=False, icon_provider=stub)
    deck.render_panel(PanelView("dev", ["online"], "grey"))
    assert seen and seen[-1] is stub


def test_desktop_runtime_passes_its_icon_provider(monkeypatch):
    seen = _capture(monkeypatch)
    stub = _StubIcons()
    app = DeckApp(MockSource(), serve=False, icon_provider=stub)
    try:
        assert seen and seen[-1] is stub
    finally:
        app.close()
