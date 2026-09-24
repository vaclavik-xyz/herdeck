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
    tile_icon: str = "agent"  # what the logo box shows ([view].tile_icon)
    project_icon: str | None = None  # resolved favicon content hash; None = monogram
    project_name: str = ""  # repo name: monogram seed (agent tiles only)
    # Rich agent-tile content (None on control tiles, which render `label` only):
    repo: str | None = None
    branch: str | None = None
    status_text: str | None = None  # WORKING / IDLE / BLOCKED / DONE
    time_text: str | None = None  # elapsed in current status, e.g. "3m"
    pinned: bool = False
    server_tag: str | None = None
    server_accent: str | None = None
    section: str | None = None  # config section a click jumps to (klik-to-jump); None = no jump
    subagents: int = 0  # running subagents: > 0 draws the fork badge (agent tiles only)


@dataclass
class PanelGauge:
    label: str
    window: str
    used_percent: int
    hint: str = ""
    color: str = "grey"
    # Usage-detail pace projection ("full ~40m early"); "" = none.
    pace: str = ""


@dataclass
class PanelStat:
    """One count card on the calm overview panel (WORKING 2, IDLE 12, ...)."""

    label: str
    value: int
    color: str


@dataclass
class PanelView:
    """The status panel (the D200's wide window, two Elgato keys, the window's
    panel strip). Every surface renders it through ``icons.compose_panel``.

    Layout, top to bottom: a header (the state chip = ``title`` on the left,
    ``meta`` on the right), a main area, and a footer (``hint`` or ``note`` on
    the left, page dots or ``aside`` on the right). The main area shows, in
    order of precedence, ``stats`` cards, ``gauges`` rows, the big
    ``headline`` with the ``lines`` under it, or — without a headline — the
    ``lines`` as body text (an agent's prompt, a menu hint).
    """

    title: str  # state chip ("Needs you", "All clear"); "" = no chip
    lines: list[str] = field(default_factory=list)  # secondary text under the headline
    color: str = "grey"  # tone: the chip colour, or the whole panel when solid
    gauges: list[PanelGauge] = field(default_factory=list)
    # A compact warning (e.g. "t3 offline" during a partial outage); takes the
    # footer's left slot in the offline tint.
    note: str = ""
    headline: str = ""  # the big main text (an agent, "Reconnecting…")
    meta: str = ""  # header right ("55s", "18 agents")
    stats: list[PanelStat] = field(default_factory=list)
    hint: str = ""  # footer left: what a press does ("press · answer")
    page: tuple[int, int] | None = None  # (index, count) -> page dots when count > 1
    aside: str = ""  # footer right accent text ("+2 more waiting"); wins over page dots
    solid: bool = False  # fill the whole panel with ``color`` (needs you, offline)
    chip_dot: str = ""  # a status dot inside a neutral chip ("All clear" -> green)
    sent: str = ""  # replaces the chip with a check mark + this text

    def cache_key(self) -> tuple:
        return (
            self.title,
            tuple(self.lines),
            self.color,
            tuple(
                (g.label, g.window, g.used_percent, g.hint, g.color, g.pace)
                for g in self.gauges
            ),
            self.note,
            self.headline,
            self.meta,
            tuple((s.label, s.value, s.color) for s in self.stats),
            self.hint,
            self.page,
            self.aside,
            self.solid,
            self.chip_dot,
            self.sent,
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
