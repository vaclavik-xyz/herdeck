from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

COLORS: dict[str, tuple[int, int, int]] = {
    "green": (40, 180, 70),
    "blue": (50, 120, 220),
    "amber": (230, 170, 20),
    "dim": (70, 70, 70),
    "red": (210, 50, 50),
    "grey": (120, 120, 120),
}


@dataclass
class TileView:
    index: int
    label: str
    color: str
    icon: str | None = None


class DeckDriver(ABC):
    @abstractmethod
    def render(self, tiles: list[TileView]) -> None:
        ...

    @abstractmethod
    def on_press(self, callback: Callable[[int], None]) -> None:
        ...

    @abstractmethod
    def slot_count(self) -> int:
        ...

    @abstractmethod
    def close(self) -> None:
        ...
