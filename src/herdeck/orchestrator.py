from __future__ import annotations

from dataclasses import dataclass, field

from . import layout
from .config import Config
from .driver.base import PanelView, TileView
from .model import AgentKey, AgentState, Status

# Panel cells (button indices 13/14) report as a press on the panel.
PANEL_INDICES = (13, 14)
_DRILL_LABELS = ["Approve", "Approve!", "Deny", "Stop", "Back"]


@dataclass
class Command:
    kind: str                 # "list" | "read" | "act_if_blocked" | "act_force"
    server_id: str
    pane_id: str | None = None
    source: str | None = None
    keys: list[str] = field(default_factory=list)


@dataclass
class RenderState:
    tiles: list[TileView]
    panel: PanelView


class Orchestrator:
    def __init__(self, config: Config, slots: int | None = None):
        self.config = config
        cols, rows = config.grid
        self.slots = slots if slots is not None else cols * rows
        self._agents: dict[AgentKey, AgentState] = {}
        self._down: set[str] = set()
        self._drill: AgentKey | None = None
        self._detection: str = ""
        self._page: int = 0
        self._phase: int = 0

    # --- inbound state ---
    def apply_snapshot(self, server_id: str, states: list[AgentState]) -> None:
        self._agents = {k: v for k, v in self._agents.items()
                        if k.server_id != server_id}
        for s in states:
            self._agents[s.key] = s

    def apply_event(self, server_id: str, state: AgentState) -> None:
        self._agents[state.key] = state

    def set_connection(self, server_id: str, up: bool) -> None:
        self._down.discard(server_id) if up else self._down.add(server_id)

    def set_detection(self, text: str) -> None:
        self._detection = text

    # --- drill helpers (used by app for read correlation) ---
    def drill_key(self) -> AgentKey | None:
        return self._drill

    def get_agent(self, key: AgentKey) -> AgentState | None:
        return self._agents.get(key)

    def is_drill_pane(self, server_id: str, pane_id: str | None) -> bool:
        return (self._drill is not None and pane_id is not None
                and self._drill == AgentKey(server_id, pane_id))

    def is_drilling(self) -> bool:
        return self._drill is not None

    # --- render ---
    def _ordered(self) -> list[AgentState]:
        return layout.order_agents(self._agents.values(), self.config.overview_order)

    def _agent_color(self, s: AgentState) -> str:
        return "red" if s.key.server_id in self._down else layout.status_color(s.status)

    def render(self) -> RenderState:
        if self._drill is not None:
            return self._render_drill()
        return self._render_overview()

    def _render_overview(self) -> RenderState:
        ordered = self._ordered()
        shown, pages = layout.page(ordered, self._page, self.slots)
        tiles: list[TileView] = []
        for i in range(self.slots):
            if i < len(shown):
                s = shown[i]
                phase = self._phase if s.status is Status.WORKING else None
                tiles.append(TileView(i, s.label, self._agent_color(s),
                                      icon=None, agent_type=s.agent_type,
                                      spinner=phase))
            else:
                tiles.append(TileView(i, "", "dim"))
        panel = layout.panel_overview(layout.summary(ordered), self._page % pages,
                                      pages, self._down)
        return RenderState(tiles, panel)

    def tick(self) -> list[int]:
        """Advance the spinner phase; return overview tile indices that are working."""
        if self._drill is not None:
            return []
        self._phase += 1
        shown, _ = layout.page(self._ordered(), self._page, self.slots)
        return [i for i, s in enumerate(shown) if s.status is Status.WORKING]

    def _render_drill(self) -> RenderState:
        agent = self._agents.get(self._drill)
        blocked = agent is not None and agent.status is Status.BLOCKED
        enabled = {0: blocked, 1: blocked, 2: blocked, 3: True, 4: True}
        colors = {0: "green", 1: "green", 2: "red", 3: "red", 4: "grey"}
        tiles: list[TileView] = []
        for i in range(self.slots):
            if i < len(_DRILL_LABELS):
                color = colors[i] if enabled[i] else "dim"
                tiles.append(TileView(i, _DRILL_LABELS[i], color))
            else:
                tiles.append(TileView(i, "", "dim"))
        panel = (layout.panel_detail(agent, self._detection)
                 if agent is not None else PanelView("", [], "grey"))
        return RenderState(tiles, panel)

    # --- presses ---
    def _profile_for(self, key: AgentKey):
        agent_type = self._agents[key].agent_type
        return self.config.profiles.get(agent_type, self.config.profiles["default"])

    def on_press(self, index: int) -> list[Command]:
        if self._drill is not None:
            return self._press_drill(index)
        return self._press_overview(index)

    def _press_overview(self, index: int) -> list[Command]:
        if index in PANEL_INDICES:
            self._page += 1
            return []
        ordered = self._ordered()
        shown, _ = layout.page(ordered, self._page, self.slots)
        if index < len(shown):
            key = shown[index].key
            self._drill = key
            self._detection = ""
            return [Command("read", key.server_id, key.pane_id, source="detection")]
        return []

    def _press_drill(self, index: int) -> list[Command]:
        key = self._drill
        if index == 4:                       # Back
            self._drill = None
            return []
        if key not in self._agents:
            self._drill = None
            return []
        agent = self._agents[key]
        if index == 3:                       # Stop — always, unconditional
            return [Command("act_force", key.server_id, key.pane_id,
                            keys=self._profile_for(key).stop)]
        if index in (0, 1, 2) and agent.status is Status.BLOCKED:
            profile = self._profile_for(key)
            keys = {0: profile.approve, 1: profile.approve_always,
                    2: profile.deny}[index]
            return [Command("act_if_blocked", key.server_id, key.pane_id, keys=keys)]
        return []                            # disabled action or blank tile
