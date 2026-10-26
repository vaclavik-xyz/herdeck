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


# What a key is, for themes (icons.compose dispatches on ``repo`` instead:
# a tile with ``repo`` set is drawn by ``Theme.agent_tile``, any other by
# ``Theme.key_tile``). "" on construction = derived in TileView.__post_init__.
TILE_KINDS: tuple[str, ...] = (
    "agent",
    "option",
    "control",
    "launcher",
    "empty",
    "idle_group",
    "pinned_hole",
    "menu",
)
PANEL_KINDS: tuple[str, ...] = (
    "calm",
    "spotlight",
    "offline",
    "config_error",
    "detail",
    "usage",
    "menu",
    "sent",
)


@dataclass(frozen=True)
class ThemeEnv:
    """What a deck theme needs from the config besides the view itself: the
    rendered language and the ``[theme.colors]`` map (status -> colour name),
    as sorted pairs so the value is hashable and part of cache keys. Stamped
    with ``deck_theme`` by ``layout.stamp_theme``."""

    language: str = "en"
    colors: tuple[tuple[str, str], ...] = ()
    project_icon_fallback: str = "monogram"


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
    # Why a WAITING agent waits ("CI", "SUB"), drawn before the elapsed time.
    # A field of its own, not part of time_text: the D200 sink treats
    # time_text as volatile, so a reason change must not hide inside it.
    wait_reason: str = ""
    pinned: bool = False
    server_tag: str | None = None
    server_accent: str | None = None
    section: str | None = None  # config section a click jumps to (klik-to-jump); None = no jump
    subagents: int = 0  # running subagents: > 0 draws the fork badge (agent tiles only)
    # --- deck themes (themes/api.py). Stamped at the render exits
    # (Orchestrator.render, ElgatoSession.render_all) via layout.stamp_theme.
    deck_theme: str = "classic"
    theme_env: ThemeEnv = field(default_factory=ThemeEnv)
    # Status value ("working", "blocked", ...) or "offline" on agent tiles, set
    # by layout.agent_tile_view; never inferred from the (configurable) colour.
    status: str | None = None
    kind: str = ""  # one of TILE_KINDS; "" = derived in __post_init__
    title: str = ""  # herdr task title (agent tiles)
    workspace: str = ""  # herdr workspace label (agent tiles)

    def __post_init__(self) -> None:
        if not self.kind:
            if self.repo is not None:
                self.kind = "agent"
            elif self.color == "empty":
                self.kind = "empty"
            elif self.color == "launcher":
                self.kind = "launcher"
            else:
                self.kind = "control"


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
class PanelAgent:
    """One blocked agent in PanelView.queue (longest-waiting first)."""

    label: str
    title: str = ""
    elapsed: str = ""
    agent_type: str = ""
    project_icon: str | None = None  # favicon content hash; None = monogram
    project_name: str = ""  # monogram seed / project_icons key


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
    # --- deck themes (themes/api.py); stamped by layout.stamp_theme.
    deck_theme: str = "classic"
    theme_env: ThemeEnv = field(default_factory=ThemeEnv)
    theme_rev: str = ""  # registry fingerprint of a non-classic theme (user file edits)
    kind: str = "calm"  # one of PANEL_KINDS
    queue: list[PanelAgent] = field(default_factory=list)  # blocked agents, longest first
    counts: dict[str, int] = field(default_factory=dict)  # overview panels; incl. "offline"

    def cache_key(self) -> tuple:
        key = (
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
        if self.deck_theme == "classic":
            # classic draws none of the theme fields: its key (and the D200's
            # panel file name, sha1(TILE_VERSION|repr(key))) stays as it was
            return key
        return key + (
            self.deck_theme,
            self.theme_rev,
            self.theme_env,
            self.kind,
            tuple(
                (a.label, a.title, a.elapsed, a.agent_type, a.project_icon, a.project_name)
                for a in self.queue
            ),
            tuple(sorted(self.counts.items())),
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


def time_line(reason: str | None, elapsed: str | None) -> str:
    """The line under a tile's status word: the waiting reason, then the
    elapsed time ("CI · 4m"). The status word itself stays WAITING so a
    waiting agent always reads as waiting, whatever holds it."""
    return " · ".join(part for part in (reason, elapsed) if part)
