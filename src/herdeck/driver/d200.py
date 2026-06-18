from __future__ import annotations

from collections.abc import Callable

from .base import COLORS, DeckDriver, TileView


class D200Driver(DeckDriver):
    """Driver for the Ulanzi D200 using the strmdck library.

    Note: the official Ulanzi desktop app must be closed or it will hold
    the USB device.
    """

    def __init__(self):
        import strmdck  # imported lazily so tests don't need hardware

        self._dev = strmdck.open_first()   # adjust to strmdck's real API
        self._dev.reset()
        self._slots = self._dev.key_count()
        self._callback: Callable[[int], None] | None = None
        self._dev.set_key_callback(self._handle_key)

    def _handle_key(self, index: int, pressed: bool) -> None:
        if pressed and self._callback is not None:
            self._callback(index)

    def render(self, tiles: list[TileView]) -> None:
        for t in tiles:
            rgb = COLORS.get(t.color, COLORS["dim"])
            image = self._dev.make_label(text=t.label, background=rgb)
            self._dev.set_key_image(t.index, image)

    def on_press(self, callback: Callable[[int], None]) -> None:
        self._callback = callback

    def slot_count(self) -> int:
        return self._slots

    def close(self) -> None:
        self._dev.close()
