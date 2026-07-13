from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

COLORS: dict[str, tuple[int, int, int]] = {
    "green": (40, 180, 70),
    "blue": (50, 120, 220),
    "amber": (230, 170, 20),
    "cyan": (45, 200, 215),
    "dim": (70, 70, 70),
    "red": (210, 50, 50),
    "grey": (120, 120, 120),
    "teal": (24, 150, 145),
    "violet": (135, 100, 235),
    "orange": (220, 115, 35),
    "pink": (215, 80, 135),
    "lime": (125, 175, 45),
    # Vacant slots: barely above the tile background — the old "dim" (70,70,70)
    # rendered empty slots BRIGHTER than occupied agent tiles on fill="none".
    "empty": (32, 32, 36),
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
    tile_fill: str = "none"  # how the tile is filled with its status colour ([view].tile_fill)
    # Rich agent-tile content (None on control tiles, which render `label` only):
    repo: str | None = None
    branch: str | None = None
    status_text: str | None = None  # WORKING / IDLE / BLOCKED / DONE
    time_text: str | None = None  # elapsed in current status, e.g. "3m"
    server_tag: str | None = None
    server_accent: str | None = None
    section: str | None = None  # config section a click jumps to (klik-to-jump); None = no jump


@dataclass
class PanelGauge:
    label: str
    window: str
    used_percent: int
    hint: str = ""
    color: str = "grey"


@dataclass
class PanelView:
    title: str
    lines: list[str] = field(default_factory=list)
    color: str = "grey"
    gauges: list[PanelGauge] = field(default_factory=list)
    gauge_meta: str = ""

    def cache_key(self) -> tuple:
        return (
            self.title,
            tuple(self.lines),
            self.color,
            tuple(
                (g.label, g.window, g.used_percent, g.hint, g.color) for g in self.gauges
            ),
            self.gauge_meta,
        )


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
