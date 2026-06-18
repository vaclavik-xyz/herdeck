from __future__ import annotations

from collections.abc import Callable

from .base import DeckDriver, TileView


class FakeRenderer(DeckDriver):
    """In-memory driver for tests and HW-less development."""

    def __init__(self, slots: int = 15):
        self._slots = slots
        self.last: list[TileView] = []
        self._callback: Callable[[int], None] | None = None

    def render(self, tiles: list[TileView]) -> None:
        self.last = tiles

    def on_press(self, callback: Callable[[int], None]) -> None:
        self._callback = callback

    def slot_count(self) -> int:
        return self._slots

    def close(self) -> None:
        pass

    def simulate_press(self, index: int) -> None:
        if self._callback is not None:
            self._callback(index)
