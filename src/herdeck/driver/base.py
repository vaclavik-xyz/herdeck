from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

COLORS: dict[str, tuple[int, int, int]] = {
    "green": (40, 180, 70),
    "blue": (50, 120, 220),
    "amber": (230, 170, 20),
    "dim": (70, 70, 70),
    "red": (210, 50, 50),
    "grey": (120, 120, 120),
    "teal": (24, 150, 145),
    "violet": (135, 100, 235),
    "orange": (220, 115, 35),
    "pink": (215, 80, 135),
    "lime": (125, 175, 45),
}


@dataclass
class TileView:
    index: int
    label: str
    color: str
    icon: str | None = None  # icon-cache filename (D200); None for fake
    subtext: str | None = None  # small wrapped text under a big label (drill choice text)
    agent_type: str | None = None
    spinner: int | None = None  # rotation phase for working tiles
    working_animation: str = "spin"  # how a working tile animates ([view].working_animation)
    # Rich agent-tile content (None on control tiles, which render `label` only):
    repo: str | None = None
    branch: str | None = None
    status_text: str | None = None  # WORKING / IDLE / BLOCKED / DONE
    time_text: str | None = None  # elapsed in current status, e.g. "3m"
    server_tag: str | None = None
    server_accent: str | None = None
    section: str | None = None  # config section a click jumps to (klik-to-jump); None = no jump


@dataclass
class PanelView:
    title: str
    lines: list[str] = field(default_factory=list)
    color: str = "grey"


class DeckDriver(ABC):
    @abstractmethod
    def render(self, tiles: list[TileView]) -> None: ...

    @abstractmethod
    def render_panel(self, panel: PanelView) -> None: ...

    @abstractmethod
    def on_press(self, callback: Callable[[int], None]) -> None: ...

    @abstractmethod
    def slot_count(self) -> int: ...

    @abstractmethod
    def close(self) -> None: ...
