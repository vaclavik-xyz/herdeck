from __future__ import annotations

import hashlib
from dataclasses import dataclass

from . import layout
from .commands import Command, profile_for
from .config import Config
from .driver.base import PanelView, TileView
from .model import AgentKey, AgentState, Status

_OPTION_LABEL_MAX = 14
SERVER_ACCENTS = ("teal", "violet", "orange", "pink", "lime")
_MANAGEMENT_ACTIONS = {"profiles", "new_agent"}


def server_accent(server_id: str, accents: list[str] | None = None) -> str:
    palette = accents or list(SERVER_ACCENTS)
    digest = hashlib.sha1(server_id.encode()).digest()
    return palette[digest[0] % len(palette)]


@dataclass
class RenderState:
    tiles: list[TileView]
    panel: PanelView


class Orchestrator:
    def __init__(self, config: Config, slots: int | None = None, clock=None):
        import time

        self.config = config
        cols, rows = config.grid
        self.slots = slots if slots is not None else cols * rows
        self._clock = clock or time.monotonic
        self._agents: dict[AgentKey, AgentState] = {}
        self._since: dict[AgentKey, tuple[Status, float]] = {}  # status start time
        self._down: set[str] = set()
        self._drill: AgentKey | None = None
        self._detection: str = ""
        self._page: int = 0
        self._phase: int = 0
        self._launcher: bool = False
        self._profile_menu: bool = False
        self._profile_menu_origin: str = "overview"

    def _agent_slots(self) -> int:
        """Overview tiles available for agents (the last tile is the launcher)."""
        if self.config.view.management == "bottom_row":
            return max(1, self.slots - 3)
        return max(1, self.slots - 1)

    def _panel_indices(self) -> tuple[int, int]:
        """The two reserved panel keys, just past the addressable tiles.

        Decks without a status window (Elgato) park the panel on the last two
        physical keys; the D200/web (slots == 13) keep the historical (13, 14).
        """
        return (self.slots, self.slots + 1)

    def _touch(self, state: AgentState) -> None:
        """Record when a pane entered its current status (for elapsed time)."""
        prev = self._since.get(state.key)
        if prev is None or prev[0] is not state.status:
            self._since[state.key] = (state.status, self._clock())

    def _elapsed_text(self, key: AgentKey) -> str:
        rec = self._since.get(key)
        if rec is None:
            return ""
        s = int(max(0, self._clock() - rec[1]))
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m"
        return f"{s // 3600}h"

    # --- inbound state ---
    def apply_snapshot(self, server_id: str, states: list[AgentState]) -> None:
        self._agents = {k: v for k, v in self._agents.items() if k.server_id != server_id}
        for s in states:
            self._agents[s.key] = s
            self._touch(s)
        live = set(self._agents)
        self._since = {k: v for k, v in self._since.items() if k in live}

    def apply_event(self, server_id: str, state: AgentState) -> None:
        self._agents[state.key] = state
        self._touch(state)

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
        return (
            self._drill is not None
            and pane_id is not None
            and self._drill == AgentKey(server_id, pane_id)
        )

    def is_drilling(self) -> bool:
        return self._drill is not None

    # --- render ---
    def _ordered(self) -> list[AgentState]:
        return layout.order_agents(self._agents.values(), self.config.overview_order)

    def _agent_color(self, s: AgentState) -> str:
        if s.key.server_id in self._down:
            return self.config.theme.colors.get("offline", "red")
        return self.config.theme.colors.get(s.status.value, layout.status_color(s.status))

    def _tile_field_enabled(self, name: str) -> bool:
        return name in self.config.view.tile_fields

    def _management_indices(self) -> dict[int, str]:
        if self.config.view.management != "bottom_row":
            return {}
        start = max(0, self.slots - 3)
        count = min(len(self.config.view.bottom_row), self.slots - start)
        actions = [
            action if action in _MANAGEMENT_ACTIONS else None
            for action in self.config.view.bottom_row[:count]
        ]
        if count and "new_agent" in self.config.view.bottom_row and "new_agent" not in actions:
            actions[-1] = "new_agent"
        return {start + i: action for i, action in enumerate(actions) if action}

    def _management_label(self, action: str) -> str:
        return {
            "profiles": "Profiles",
            "notifications": "Notify",
            "safety": "Safety",
            "theme": "Theme",
            "new_agent": "+ New",
        }.get(action, action)

    def _blocked_spotlight(self) -> tuple[str, str] | None:
        """The longest-waiting BLOCKED agent as (label, elapsed), or None."""
        blocked = [s for s in self._agents.values() if s.status is Status.BLOCKED]
        if not blocked:
            return None

        def started(s):
            rec = self._since.get(s.key)
            return rec[1] if rec else 0.0

        oldest = min(blocked, key=started)
        return (oldest.label, self._elapsed_text(oldest.key))

    def render(self) -> RenderState:
        if self._profile_menu:
            return self._render_profile_menu()
        if self._launcher:
            return self._render_launcher()
        if self._drill is not None:
            return self._render_drill()
        return self._render_overview()

    def _render_overview(self) -> RenderState:
        ordered = self._ordered()
        agent_slots = self._agent_slots()
        shown, pages = layout.page(ordered, self._page, agent_slots)
        fields = self.config.view.tile_fields
        show_server_tags = "server" in fields and len({s.key.server_id for s in ordered}) > 1
        management = self._management_indices()
        management_mode = self.config.view.management == "bottom_row"
        tiles: list[TileView] = []
        for i in range(self.slots):
            if i in management:
                tiles.append(TileView(i, self._management_label(management[i]), "grey"))
            elif not management_mode and i == self.slots - 1:  # reserved launcher tile
                tiles.append(TileView(i, "+ New", "green"))
            elif i < len(shown):
                s = shown[i]
                phase = self._phase if s.status is Status.WORKING else None
                down = s.key.server_id in self._down
                tag = s.key.server_id[:3].upper() if show_server_tags else None
                accent = (
                    server_accent(s.key.server_id, self.config.theme.server_accents)
                    if show_server_tags
                    else None
                )
                tiles.append(
                    TileView(
                        i,
                        s.label,
                        self._agent_color(s),
                        icon=None,
                        agent_type=s.agent_type,
                        spinner=phase,
                        repo=(s.repo or s.label) if "repo" in fields else "",
                        branch=(s.branch or "") if "branch" in fields else "",
                        status_text=(
                            "OFFLINE" if down else s.status.value.upper()
                        )
                        if "status" in fields
                        else None,
                        time_text=self._elapsed_text(s.key) if "time" in fields else None,
                        server_tag=tag,
                        server_accent=accent,
                    )
                )
            else:
                tiles.append(TileView(i, "", "dim"))
        panel = layout.panel_overview(
            layout.summary(ordered),
            self._page % pages,
            pages,
            self._down,
            len(ordered),
            self._blocked_spotlight(),
        )
        if panel.color == "red":
            panel.color = self.config.theme.colors.get("offline", panel.color)
        elif panel.color == "amber":
            panel.color = self.config.theme.colors.get("blocked", panel.color)
        return RenderState(tiles, panel)

    def _render_profile_menu(self) -> RenderState:
        names = list(self.config.meta.profile_names)
        back_i = self.slots - 1
        tiles: list[TileView] = []
        for i in range(self.slots):
            if i < len(names) and i < back_i:
                name = names[i]
                label = f"* {name}" if name == self.config.meta.active_profile else name
                tiles.append(TileView(i, label[:_OPTION_LABEL_MAX], "blue"))
            elif i == back_i:
                tiles.append(TileView(i, "Back", "grey"))
            else:
                tiles.append(TileView(i, "", "dim"))
        locked = "locked by env" if self.config.meta.env_locked_profile else "pick a profile"
        return RenderState(tiles, PanelView("profiles", [locked], "grey"))

    def _render_launcher(self) -> RenderState:
        types = list(self.config.start_profiles)
        entries = types + (["Profiles"] if len(self.config.meta.profile_names) > 1 else [])
        back_i = self.slots - 1
        tiles: list[TileView] = []
        for i in range(self.slots):
            if i < len(entries) and i < back_i:
                entry = entries[i]
                agent_type = entry if entry in self.config.start_profiles else None
                tiles.append(TileView(i, entry, "blue", agent_type=agent_type))
            elif i == back_i:
                tiles.append(TileView(i, "Back", "grey"))
            else:
                tiles.append(TileView(i, "", "dim"))
        return RenderState(tiles, PanelView("new agent", ["pick a type"], "grey"))

    def tick(self) -> list[int]:
        """Advance the spinner phase; return overview tile indices that are working."""
        if self._drill is not None or self._launcher or self._profile_menu:
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
            options = layout.parse_options(self._detection)
            if options:
                for opt in options:
                    actions.append(
                        {
                            "label": f"{opt.key} {opt.label}"[:_OPTION_LABEL_MAX],
                            "make": (
                                lambda key, k=opt.key: Command(
                                    "act_if_blocked", key.server_id, key.pane_id, keys=[k]
                                )
                            ),
                        }
                    )
            elif self._detection.strip():
                # Read completed but no numbered options (e.g. a y/n prompt): fall
                # back to the agent's configured Approve / Approve! / Deny keys.
                # (Skipped while detection is empty so we never offer blind
                # approval before the prompt has been read.)
                profile = self._profile_for(self._drill)
                for label, keys in (
                    ("Approve", profile.approve),
                    ("Approve!", profile.approve_always),
                    ("Deny", profile.deny),
                ):
                    actions.append(
                        {
                            "label": label,
                            "make": (
                                lambda key, ks=keys: Command(
                                    "act_if_blocked", key.server_id, key.pane_id, keys=ks
                                )
                            ),
                        }
                    )
        elif agent is not None:
            for m in self.config.macros:
                actions.append(
                    {
                        "label": m.label[:_OPTION_LABEL_MAX],
                        "make": (
                            lambda key, t=m.text: Command(
                                "send_text", key.server_id, key.pane_id, text=t
                            )
                        ),
                    }
                )
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
        panel = (
            layout.panel_detail(agent, self._detection)
            if agent is not None
            else PanelView("", [], "grey")
        )
        return RenderState(tiles, panel)

    # --- presses ---
    def _profile_for(self, key: AgentKey):
        return profile_for(self.config, self._agents[key].agent_type)

    def on_press(self, index: int) -> list[Command]:
        if self._profile_menu:
            return self._press_profile_menu(index)
        if self._launcher:
            return self._press_launcher(index)
        if self._drill is not None:
            return self._press_drill(index)
        return self._press_overview(index)

    def _press_overview(self, index: int) -> list[Command]:
        if index in self._panel_indices():
            self._page += 1
            return []
        management = self._management_indices()
        if index in management:
            action = management[index]
            if action == "profiles":
                self._profile_menu = True
                self._profile_menu_origin = "overview"
            elif action == "new_agent":
                self._launcher = True
            return []
        if self.config.view.management != "bottom_row" and index == self.slots - 1:
            self._launcher = True
            return []
        ordered = self._ordered()
        shown, _ = layout.page(ordered, self._page, self._agent_slots())
        if index < len(shown):
            key = shown[index].key
            self._drill = key
            self._detection = ""
            # Focus the agent in the on-screen herdr session AND read its prompt.
            return [
                Command("focus", key.server_id, key.pane_id),
                Command("read", key.server_id, key.pane_id, source="detection"),
            ]
        return []

    def _press_launcher(self, index: int) -> list[Command]:
        types = list(self.config.start_profiles)
        entries = types + (["Profiles"] if len(self.config.meta.profile_names) > 1 else [])
        back_i = self.slots - 1
        if index == back_i:
            self._launcher = False
            return []
        if index < len(entries) and index < back_i:
            name = entries[index]
            if name == "Profiles":
                self._profile_menu = True
                self._profile_menu_origin = "launcher"
                self._launcher = False
                return []
            argv = list(self.config.start_profiles[name])
            server = self.config.overview_order[0]
            self._launcher = False  # return to overview
            return [Command("start", server, text=name, keys=argv)]
        return []

    def _press_profile_menu(self, index: int) -> list[Command]:
        names = list(self.config.meta.profile_names)
        back_i = self.slots - 1
        if index == back_i:
            self._profile_menu = False
            self._launcher = self._profile_menu_origin == "launcher"
            return []
        if index < len(names) and index < back_i:
            name = names[index]
            self._profile_menu = False
            self._profile_menu_origin = "overview"
            self._launcher = False
            return [Command("switch_profile", name, text=name)]
        return []

    def _press_drill(self, index: int) -> list[Command]:
        key = self._drill
        actions, stop_i, back_i = self._drill_layout()
        if index == back_i:  # Back to overview
            self._drill = None
            return []
        if key not in self._agents:
            self._drill = None
            return []
        if index == stop_i:  # Stop — always, unconditional
            cmd = Command("act_force", key.server_id, key.pane_id, keys=self._profile_for(key).stop)
            self._drill = None  # return to the fleet overview
            return [cmd]
        if index < len(actions):  # send option number or macro text
            cmd = actions[index]["make"](key)
            self._drill = None  # return to the fleet overview
            return [cmd]
        return []  # blank tile

    def update_config(self, config: Config) -> None:
        self.config = config
        allowed_servers = {s.id for s in config.servers}
        self._agents = {
            key: state for key, state in self._agents.items() if key.server_id in allowed_servers
        }
        self._since = {
            key: value for key, value in self._since.items() if key.server_id in allowed_servers
        }
        self._down &= allowed_servers
        self._launcher = False
        self._profile_menu = False
        self._profile_menu_origin = "overview"
        self._drill = None
        self._detection = ""
        self._page = 0

    def clear_server_state(self, server_ids) -> None:
        server_ids = set(server_ids)
        self._agents = {
            key: state for key, state in self._agents.items() if key.server_id not in server_ids
        }
        self._since = {
            key: value for key, value in self._since.items() if key.server_id not in server_ids
        }
        if self._drill is not None and self._drill.server_id in server_ids:
            self._drill = None
            self._detection = ""
