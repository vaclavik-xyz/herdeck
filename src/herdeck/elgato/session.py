from __future__ import annotations

import time
from dataclasses import dataclass

from ..config import Config
from ..driver.base import TileView
from ..model import AgentKey, AgentState, Status
from .slots import SlotLeases

_STATUS_COLOR = {
    Status.WORKING: "green",
    Status.IDLE: "blue",
    Status.BLOCKED: "amber",
    Status.DONE: "dim",
}


@dataclass
class KeyRender:
    image_png: bytes
    title: str | None = None


class ElgatoSession:
    def __init__(self, config: Config, icons, *, clock=None, arm_timeout: float = 3.0) -> None:
        self.config = config
        self._icons = icons
        self._clock = clock or time.monotonic
        self._arm_timeout = arm_timeout
        self._agents: dict[AgentKey, AgentState] = {}
        self._down: set[str] = set()
        self._leases = SlotLeases()
        self._slot_order: list[str] = []  # slot instance_ids in reading order
        self._slot_coords: dict[str, tuple[int, int]] = {}

    # --- inbound agent state ---
    def apply_snapshot(self, server_id: str, states: list[AgentState]) -> None:
        self._agents = {k: v for k, v in self._agents.items() if k.server_id != server_id}
        for s in states:
            self._agents[s.key] = s
        self._release()

    def apply_event(self, server_id: str, state: AgentState) -> None:
        self._agents[state.key] = state
        self._release()

    def set_connection(self, server_id: str, up: bool) -> None:
        self._down.discard(server_id) if up else self._down.add(server_id)

    # --- layout ---
    def set_slots(self, instances: list[tuple[str, tuple[int, int]]]) -> None:
        self._slot_coords = {iid: coord for iid, coord in instances}
        self._slot_order = [iid for iid, _ in sorted(instances, key=lambda t: (t[1][1], t[1][0]))]
        self._release()

    # --- internals ---
    def _server_rank(self, server_id: str) -> int:
        try:
            return self.config.overview_order.index(server_id)
        except ValueError:
            return len(self.config.overview_order)

    def _release(self) -> None:
        ordered = sorted(self._agents.values(), key=lambda s: (self._server_rank(s.key.server_id), s.key.pane_id))
        self._leases.update([s.key for s in ordered])

    def _color(self, s: AgentState) -> str:
        if s.key.server_id in self._down:
            return "red"
        return _STATUS_COLOR.get(s.status, "grey")

    def _slot_tile(self, ordinal: int) -> TileView:
        key = self._leases.assignment().get(ordinal)
        if key is None:
            return TileView(ordinal, "", "dim")
        s = self._agents[key]
        down = s.key.server_id in self._down
        return TileView(
            ordinal,
            s.label,
            self._color(s),
            agent_type=s.agent_type,
            repo=s.repo or s.label,
            branch=s.branch or "",
            status_text="OFFLINE" if down else s.status.value.upper(),
        )

    # --- render ---
    def render_all(self) -> dict[str, KeyRender]:
        out: dict[str, KeyRender] = {}
        for ordinal, iid in enumerate(self._slot_order):
            tile = self._slot_tile(ordinal)
            out[iid] = KeyRender(self._icons.render_tile_bytes(tile))
        return out
