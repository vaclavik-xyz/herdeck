from __future__ import annotations

from dataclasses import dataclass, field

from . import layout
from .config import Config
from .driver.base import PanelView, TileView
from .model import AgentKey, AgentState, Status

# Panel cells (button indices 13/14) report as a press on the panel.
PANEL_INDICES = (13, 14)
_OPTION_LABEL_MAX = 14


@dataclass
class Command:
    kind: str                 # list|read|focus|act_if_blocked|act_force|send_text|start
    server_id: str
    pane_id: str | None = None
    source: str | None = None
    keys: list[str] = field(default_factory=list)
    text: str | None = None   # for send_text (macros)


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
        self._launcher: bool = False

    def _agent_slots(self) -> int:
        """Overview tiles available for agents (the last tile is the launcher)."""
        return max(1, self.slots - 1)

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
        if self._launcher:
            return self._render_launcher()
        if self._drill is not None:
            return self._render_drill()
        return self._render_overview()

    def _render_overview(self) -> RenderState:
        ordered = self._ordered()
        agent_slots = self._agent_slots()
        shown, pages = layout.page(ordered, self._page, agent_slots)
        tiles: list[TileView] = []
        for i in range(self.slots):
            if i == self.slots - 1:                 # reserved launcher tile
                tiles.append(TileView(i, "+ New", "green"))
            elif i < len(shown):
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

    def _render_launcher(self) -> RenderState:
        types = list(self.config.start_profiles)
        back_i = self.slots - 1
        tiles: list[TileView] = []
        for i in range(self.slots):
            if i < len(types) and i < back_i:
                tiles.append(TileView(i, types[i], "blue", agent_type=types[i]))
            elif i == back_i:
                tiles.append(TileView(i, "Back", "grey"))
            else:
                tiles.append(TileView(i, "", "dim"))
        return RenderState(tiles, PanelView("new agent", ["pick a type"], "grey"))

    def tick(self) -> list[int]:
        """Advance the spinner phase; return overview tile indices that are working."""
        if self._drill is not None or self._launcher:
            return []
        self._phase += 1
        shown, _ = layout.page(self._ordered(), self._page, self._agent_slots())
        return [i for i, s in enumerate(shown) if s.status is Status.WORKING]

    def _drill_layout(self) -> tuple[list, int, int]:
        """Drill action tiles plus the fixed Stop/Back indices.

        Blocked agent -> parsed prompt options (send the number). Otherwise ->
        configured quick-send macros (send text). Each action is a dict with a
        ``label`` and a callable ``make`` that builds the Command for a key.
        """
        agent = self._agents.get(self._drill)
        stop_i, back_i = self.slots - 2, self.slots - 1
        actions: list[dict] = []
        if agent is not None and agent.status is Status.BLOCKED:
            for opt in layout.parse_options(self._detection):
                actions.append({
                    "label": f"{opt.key} {opt.label}"[:_OPTION_LABEL_MAX],
                    "make": (lambda key, k=opt.key: Command(
                        "act_if_blocked", key.server_id, key.pane_id, keys=[k])),
                })
        elif agent is not None:
            for m in self.config.macros:
                actions.append({
                    "label": m.label[:_OPTION_LABEL_MAX],
                    "make": (lambda key, t=m.text: Command(
                        "send_text", key.server_id, key.pane_id, text=t)),
                })
        return actions[:stop_i], stop_i, back_i

    def _render_drill(self) -> RenderState:
        agent = self._agents.get(self._drill)
        actions, stop_i, back_i = self._drill_layout()
        tiles: list[TileView] = []
        for i in range(self.slots):
            if i < len(actions):
                tiles.append(TileView(i, actions[i]["label"], "blue"))
            elif i == stop_i:
                tiles.append(TileView(i, "Stop", "red"))
            elif i == back_i:
                tiles.append(TileView(i, "Back", "grey"))
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
        if self._launcher:
            return self._press_launcher(index)
        if self._drill is not None:
            return self._press_drill(index)
        return self._press_overview(index)

    def _press_overview(self, index: int) -> list[Command]:
        if index in PANEL_INDICES:
            self._page += 1
            return []
        if index == self.slots - 1:             # "+ New" launcher tile
            self._launcher = True
            return []
        ordered = self._ordered()
        shown, _ = layout.page(ordered, self._page, self._agent_slots())
        if index < len(shown):
            key = shown[index].key
            self._drill = key
            self._detection = ""
            # Focus the agent in the on-screen herdr session AND read its prompt.
            return [Command("focus", key.server_id, key.pane_id),
                    Command("read", key.server_id, key.pane_id, source="detection")]
        return []

    def _press_launcher(self, index: int) -> list[Command]:
        types = list(self.config.start_profiles)
        back_i = self.slots - 1
        if index == back_i:
            self._launcher = False
            return []
        if index < len(types) and index < back_i:
            name = types[index]
            argv = list(self.config.start_profiles[name])
            server = self.config.overview_order[0]
            self._launcher = False              # return to overview
            return [Command("start", server, text=name, keys=argv)]
        return []

    def _press_drill(self, index: int) -> list[Command]:
        key = self._drill
        actions, stop_i, back_i = self._drill_layout()
        if index == back_i:                  # Back to overview
            self._drill = None
            return []
        if key not in self._agents:
            self._drill = None
            return []
        if index == stop_i:                  # Stop — always, unconditional
            cmd = Command("act_force", key.server_id, key.pane_id,
                          keys=self._profile_for(key).stop)
            self._drill = None               # return to the fleet overview
            return [cmd]
        if index < len(actions):             # send option number or macro text
            cmd = actions[index]["make"](key)
            self._drill = None               # return to the fleet overview
            return [cmd]
        return []                            # blank tile
