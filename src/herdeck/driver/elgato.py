from __future__ import annotations

from collections.abc import Callable

from .base import DeckDriver, PanelView, TileView

BRIGHTNESS = 80
# Keys reserved for the status panel (Elgato decks have no separate window).
_PANEL_KEYS = 2


class ElgatoDriver(DeckDriver):
    """Driver for Elgato Stream Deck hardware (python-elgato-streamdeck).

    The last two physical keys are reserved for the status panel, mirroring the
    D200's 13 tiles + 2-cell window. Pass ``device`` to inject a fake for tests;
    when it is None a real deck is enumerated and opened.
    """

    def __init__(self, device=None, icon_provider=None):
        self._dev = device if device is not None else self._open_device()
        self._icons = icon_provider
        self._callback: Callable[[int], None] | None = None

    def _open_device(self):
        # Hardware path (lazy import so the test suite needs neither the library
        # nor a physical deck).
        from StreamDeck.DeviceManager import DeviceManager
        decks = DeviceManager().enumerate()
        if not decks:
            raise RuntimeError("No Elgato Stream Deck found")
        deck = decks[0]
        deck.open()
        deck.reset()
        deck.set_brightness(BRIGHTNESS)
        return deck

    def slot_count(self) -> int:
        return self._dev.key_count() - _PANEL_KEYS

    def render(self, tiles: list[TileView]) -> None:
        pass

    def render_panel(self, panel: PanelView) -> None:
        pass

    def on_press(self, callback: Callable[[int], None]) -> None:
        pass

    def close(self) -> None:
        import contextlib
        with contextlib.suppress(Exception):
            self._dev.close()
