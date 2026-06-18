from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .driver.base import TileView
from .model import AgentKey, AgentState, Status

# Default system-tile indices for a 15-slot (5x3) deck. The orchestrator
# computes its own per-instance indices from the real slot count (see __init__);
# these module constants are the 15-slot reference used by tests.
SLOT_NEXT = 12
SLOT_REFRESH = 13
SLOT_CONN = 14


@dataclass
class Command:
    kind: str                 # "list" | "read" | "act_if_blocked"
    server_id: str
    pane_id: str | None = None
    source: str | None = None
    keys: list[str] = field(default_factory=list)


_STATUS_COLOR = {
    Status.WORKING: "green",
    Status.IDLE: "blue",
    Status.BLOCKED: "amber",
    Status.DONE: "dim",
    Status.UNKNOWN: "grey",
}


class Orchestrator:
    def __init__(self, config: Config, slots: int | None = None):
        self.config = config
        self.cols, self.rows = config.grid
        # The real button count comes from the deck driver; fall back to the
        # grid product when not provided (e.g. in isolated unit tests).
        self.slots = slots if slots is not None else self.cols * self.rows
        # Last three slots are the system tiles (Next / Refresh / Link).
        self.slot_next = self.slots - 3
        self.slot_refresh = self.slots - 2
        self.slot_conn = self.slots - 1
        self._agents: dict[AgentKey, AgentState] = {}
        self._down: set[str] = set()
        self._drill: AgentKey | None = None
        self._detection: str = ""

    # --- inbound state ---
    def apply_snapshot(self, server_id: str, states: list[AgentState]) -> None:
        self._agents = {
            k: v for k, v in self._agents.items() if k.server_id != server_id
        }
        for s in states:
            self._agents[s.key] = s

    def apply_event(self, server_id: str, state: AgentState) -> None:
        self._agents[state.key] = state

    def set_detection(self, text: str) -> None:
        self._detection = text

    def drill_key(self) -> AgentKey | None:
        return self._drill

    def get_agent(self, key: AgentKey) -> AgentState | None:
        return self._agents.get(key)

    def is_drill_pane(self, server_id: str, pane_id: str | None) -> bool:
        return (self._drill is not None and pane_id is not None
                and self._drill == AgentKey(server_id, pane_id))

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
            key=lambda s: (order.get(s.key.server_id, 999), s.key.pane_id),
        )

    def _agent_color(self, s: AgentState) -> str:
        if s.key.server_id in self._down:
            return "red"
        return _STATUS_COLOR[s.status]

    # --- render ---
    def render(self) -> list[TileView]:
        if self._drill is not None:
            return self._render_drill()
        return self._render_overview()

    _DRILL_LABELS = ["Approve", "Approve!", "Deny", "Stop", "Back"]

    def _render_drill(self) -> list[TileView]:
        tiles = []
        for i in range(self.slots):
            if i < len(self._DRILL_LABELS):
                label = self._DRILL_LABELS[i]
            elif i == 5 and self._detection:
                label = self._detection[:40]
            else:
                label = ""
            color = {0: "green", 1: "green", 2: "red", 3: "red",
                     4: "grey"}.get(i, "dim")
            tiles.append(TileView(i, label, color))
        return tiles

    def _profile_for(self, key: AgentKey):
        agent_type = self._agents[key].agent_type
        return self.config.profiles.get(agent_type, self.config.profiles["default"])

    def on_press(self, index: int) -> list[Command]:
        if self._drill is not None:
            return self._press_drill(index)
        return self._press_overview(index)

    def _first_blocked(self) -> AgentState | None:
        for s in self._ordered_agents():
            if s.status is Status.BLOCKED:
                return s
        return None

    def _enter_drill(self, key: AgentKey) -> list[Command]:
        self._drill = key
        self._detection = ""
        return [Command("read", key.server_id, key.pane_id, source="detection")]

    def _press_overview(self, index: int) -> list[Command]:
        if index == self.slot_refresh:
            sids = {s.server_id for s in self._agents} or {
                s.id for s in self.config.servers
            }
            return [Command("list", sid) for sid in sorted(sids)]
        if index == self.slot_next:
            s = self._first_blocked()
            return self._enter_drill(s.key) if s else []
        if index == self.slot_conn:
            return []
        agents = self._ordered_agents()
        if index < len(agents):
            s = agents[index]
            if s.status is Status.BLOCKED:
                return self._enter_drill(s.key)
        return []

    def _press_drill(self, index: int) -> list[Command]:
        key = self._drill
        if index == 4:                       # Back
            self._drill = None
            return []
        if key not in self._agents:
            self._drill = None
            return []
        profile = self._profile_for(key)
        keymap = {0: profile.approve, 1: profile.approve_always,
                  2: profile.deny, 3: profile.stop}
        keys = keymap.get(index)
        if keys is None:
            return []
        self._drill = None
        return [Command("act_if_blocked", key.server_id, key.pane_id, keys=keys)]

    def _render_overview(self) -> list[TileView]:
        tiles: list[TileView] = []
        agents = self._ordered_agents()
        for i in range(self.slot_next):  # agent slots 0 .. slot_next-1
            if i < len(agents):
                s = agents[i]
                tiles.append(TileView(i, s.label, self._agent_color(s)))
            else:
                tiles.append(TileView(i, "", "dim"))
        conn_color = "red" if self._down else "green"
        tiles.append(TileView(self.slot_next, "Next", "grey"))
        tiles.append(TileView(self.slot_refresh, "Refresh", "grey"))
        tiles.append(TileView(self.slot_conn, "Link", conn_color))
        return tiles
