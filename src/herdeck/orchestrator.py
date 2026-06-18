from __future__ import annotations

from .config import Config
from .driver.base import TileView
from .model import AgentKey, AgentState, Status

SLOT_NEXT = 12
SLOT_REFRESH = 13
SLOT_CONN = 14

_STATUS_COLOR = {
    Status.WORKING: "green",
    Status.IDLE: "blue",
    Status.BLOCKED: "amber",
    Status.DONE: "dim",
    Status.UNKNOWN: "grey",
}


class Orchestrator:
    def __init__(self, config: Config):
        self.config = config
        self.cols, self.rows = config.grid
        self.slots = self.cols * self.rows
        self._agents: dict[AgentKey, AgentState] = {}
        self._down: set[str] = set()

    # --- inbound state ---
    def apply_snapshot(self, server_id: str, states: list[AgentState]) -> None:
        self._agents = {
            k: v for k, v in self._agents.items() if k.server_id != server_id
        }
        for s in states:
            self._agents[s.key] = s

    def apply_event(self, server_id: str, state: AgentState) -> None:
        self._agents[state.key] = state

    def set_connection(self, server_id: str, up: bool) -> None:
        if up:
            self._down.discard(server_id)
        else:
            self._down.add(server_id)

    # --- ordering ---
    def _ordered_agents(self) -> list[AgentState]:
        order = {sid: i for i, sid in enumerate(self.config.overview_order)}
        return sorted(
            self._agents.values(),
            key=lambda s: (order.get(s.key.server_id, 999), s.label, s.key.pane_id),
        )

    def _agent_color(self, s: AgentState) -> str:
        if s.key.server_id in self._down:
            return "red"
        return _STATUS_COLOR[s.status]

    # --- render ---
    def render(self) -> list[TileView]:
        return self._render_overview()

    def _render_overview(self) -> list[TileView]:
        tiles: list[TileView] = []
        agents = self._ordered_agents()
        for i in range(SLOT_NEXT):  # agent slots 0..11
            if i < len(agents):
                s = agents[i]
                tiles.append(TileView(i, s.label, self._agent_color(s)))
            else:
                tiles.append(TileView(i, "", "dim"))
        conn_color = "red" if self._down else "green"
        tiles.append(TileView(SLOT_NEXT, "Next", "grey"))
        tiles.append(TileView(SLOT_REFRESH, "Refresh", "grey"))
        tiles.append(TileView(SLOT_CONN, "Link", conn_color))
        return tiles
