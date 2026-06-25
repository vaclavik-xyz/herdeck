from __future__ import annotations

import time
from dataclasses import dataclass

from .. import layout
from ..commands import Command, build_action_command, profile_for
from ..config import Config
from ..driver.base import TileView
from ..model import AgentKey, AgentState, Status
from .slots import SlotLeases


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
        self._manual: AgentKey | None = None
        self._action_keys: list[tuple[str, str]] = []   # (instance_id, type)
        self._detection: dict[AgentKey, str] = {}
        self._block_gen: dict[AgentKey, int] = {}  # +1 each time an agent enters BLOCKED
        self._pending_act: AgentKey | None = None  # an act is in flight for this agent
        self._armed_for: AgentKey | None = None
        self._armed_at: float = 0.0
        self._last_bytes: dict[str, bytes] = {}

    # --- inbound agent state ---
    def apply_snapshot(self, server_id: str, states: list[AgentState]) -> None:
        self._bump_block_gen(states)
        self._agents = {k: v for k, v in self._agents.items() if k.server_id != server_id}
        for s in states:
            self._agents[s.key] = s
        if self._pending_act is not None and self._pending_act.server_id == server_id:
            self._pending_act = None
        self._release()
        self._prune_detection()
        self._reconcile_arm()

    def apply_event(self, server_id: str, state: AgentState) -> None:
        self._bump_block_gen([state])
        self._agents[state.key] = state
        if state.key == self._pending_act:
            self._pending_act = None
        self._release()
        self._prune_detection()
        self._reconcile_arm()

    def set_connection(self, server_id: str, up: bool) -> None:
        if up:
            self._down.discard(server_id)
        else:
            self._down.add(server_id)
            # Drop cached prompts for the dropped server so a stale prompt cannot
            # re-enable Approve/Deny after reconnect; the proactive read re-populates.
            self._detection = {
                k: v for k, v in self._detection.items() if k.server_id != server_id
            }
            # Clear an in-flight act marker for the dropped server, else its action key
            # renders PENDING for the entire outage — no result will arrive to clear it
            # until reconnect+snapshot.
            if self._pending_act is not None and self._pending_act.server_id == server_id:
                self._pending_act = None
        self._reconcile_arm()

    # --- selection ---
    def select(self, key: AgentKey | None) -> None:
        self._manual = key
        self._reconcile_arm()

    # --- stop arm-then-confirm ---
    def _arm(self) -> None:
        self._armed_for = self.selected()
        self._armed_at = self._clock()

    def is_armed(self) -> bool:
        return (
            self._armed_for is not None
            and self._armed_for == self.selected()
            and (self._clock() - self._armed_at) <= self._arm_timeout
        )

    def _reconcile_arm(self) -> None:
        # Drop a stale arm when the effective target changes/vanishes, OR when the
        # armed target's server goes offline (Stop is disabled offline, so a lingering
        # arm would render a phantom STOP? and a quick reconnect could fire it).
        if self._armed_for is not None and (
            self._armed_for != self.selected()
            or self._armed_for.server_id in self._down
        ):
            self._armed_for = None

    def tick(self) -> None:
        if self._armed_for is not None and self._clock() - self._armed_at > self._arm_timeout:
            self._armed_for = None

    def now(self) -> float:
        """Current time on the session's injectable clock (shared by the read
        correlator's blank-read backoff so both use the same time base)."""
        return self._clock()

    def selected(self) -> AgentKey | None:
        if self._manual is not None and self._manual in self._agents:
            return self._manual
        self._manual = None
        blocked = [k for k, s in self._agents.items() if s.status is Status.BLOCKED]
        return blocked[0] if len(blocked) == 1 else None

    # --- action keys / detection ---
    def set_action_keys(self, instances: list[tuple[str, str, tuple[int, int]]]) -> None:
        self._action_keys = [(iid, kind) for iid, kind, _ in instances]

    def set_detection(self, key: AgentKey, text: str) -> bool:
        # Only trust a prompt read for an agent that is present and currently blocked.
        # A blank read is not a prompt: storing it would mark the agent "read" and
        # silence the proactive re-read, leaving Approve stuck disabled forever.
        # Returns whether the prompt was stored, so the read correlator can keep a
        # pending marker on a blank read instead of immediately re-reading (spin).
        agent = self._agents.get(key)
        if agent is not None and agent.status is Status.BLOCKED and text.strip():
            self._detection[key] = text
            return True
        return False

    def _prune_detection(self) -> None:
        # Drop cached prompts whose agent vanished or is no longer blocked, so stale
        # prompt text can never re-enable Approve/Deny for a changed/recreated agent.
        self._detection = {
            k: v
            for k, v in self._detection.items()
            if k in self._agents and self._agents[k].status is Status.BLOCKED
        }

    def block_generation(self, key: AgentKey) -> int:
        return self._block_gen.get(key, 0)

    def blocked_without_detection(self) -> list[AgentKey]:
        # Blocked agents on an ONLINE server whose prompt has not been read yet — the
        # runtime issues a proactive read for each so Approve can enable without a
        # slot press. Offline servers are skipped: reading a dead connector would just
        # leave a pending read that suppresses the real read after reconnect.
        return [
            k for k, s in self._agents.items()
            if s.status is Status.BLOCKED
            and k.server_id not in self._down
            and k not in self._detection
        ]

    def _bump_block_gen(self, incoming: list[AgentState]) -> None:
        # A fresh BLOCKED episode increments the generation so the runtime read
        # correlator can reject a read that was issued for an earlier episode.
        for s in incoming:
            prev = self._agents.get(s.key)
            if s.status is Status.BLOCKED and (prev is None or prev.status is not Status.BLOCKED):
                self._block_gen[s.key] = self._block_gen.get(s.key, 0) + 1

    def _target(self) -> AgentState | None:
        key = self.selected()
        return self._agents.get(key) if key is not None else None

    def action_enabled(self, kind: str) -> bool:
        if kind == "pager":
            return True
        target = self._target()
        if target is None or target.key.server_id in self._down:
            return False
        if kind == "stop":
            return True
        if kind in ("approve", "deny"):
            if target.status is not Status.BLOCKED:
                return False
            text = self._detection.get(target.key)
            if not text or not text.strip():
                return False
            return not layout.parse_options(text)
        return False

    def _action_tile(self, instance_id: str, kind: str) -> TileView:
        enabled = self.action_enabled(kind)
        target = self._target()
        labels = {"approve": "Approve", "deny": "Deny", "stop": "Stop", "pager": "Next"}
        ident = (target.repo or target.label) if (target is not None and kind != "pager") else ""
        color = {"approve": "green", "deny": "amber", "stop": "red", "pager": "blue"}[kind]
        if kind == "stop" and self.is_armed():
            return TileView(0, "Stop", "red", repo=ident or None, status_text="STOP?")
        if kind != "pager" and target is not None and target.key == self._pending_act:
            return TileView(0, labels[kind], "dim", repo=ident or None, status_text="PENDING")
        return TileView(
            0,
            labels[kind],
            color if enabled else "dim",
            repo=ident or None,
            status_text=labels[kind].upper(),
        )

    # --- press handling ---
    def _slot_instance_key(self, instance_id: str) -> AgentKey | None:
        if instance_id not in self._slot_order:
            return None
        ordinal = self._slot_order.index(instance_id)
        return self._leases.assignment().get(ordinal)

    def _action_kind(self, instance_id: str) -> str | None:
        for iid, kind in self._action_keys:
            if iid == instance_id:
                return kind
        return None

    def key_up(self, instance_id: str) -> list[Command]:
        key = self._slot_instance_key(instance_id)
        if key is not None:
            self.select(key)
            return [
                Command("focus", key.server_id, key.pane_id),
                Command("read", key.server_id, key.pane_id, source="detection"),
            ]
        kind = self._action_kind(instance_id)
        if kind == "pager":
            self._page_blocked()
            return []
        if kind is None or not self.action_enabled(kind):
            return []
        target = self._target()
        if self._pending_act == target.key:
            return []  # an act is already in flight for this agent — never double-send
        profile = profile_for(self.config, target.agent_type)
        if kind in ("approve", "deny"):
            self._pending_act = target.key  # show pending until the next state update
            return [build_action_command(kind, target, profile, force=False, always=False)]
        if kind == "stop":
            if not self.is_armed():
                self._arm()
                return []
            self._armed_for = None
            self._pending_act = target.key
            return [build_action_command("stop", target, profile, force=True, always=False)]
        return []

    def _page_blocked(self) -> None:
        blocked = sorted(
            (k for k, s in self._agents.items() if s.status is Status.BLOCKED),
            key=lambda k: (self._server_rank(k.server_id), k.pane_id),
        )
        if not blocked:
            return
        cur = self.selected()
        idx = blocked.index(cur) + 1 if cur in blocked else 0
        self.select(blocked[idx % len(blocked)])

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
            return self.config.theme.colors.get("offline", "red")
        return self.config.theme.colors.get(s.status.value, layout.status_color(s.status))

    def _slot_tile(self, ordinal: int) -> TileView:
        key = self._leases.assignment().get(ordinal)
        if key is None:
            return TileView(ordinal, "", "dim")
        s = self._agents[key]
        down = s.key.server_id in self._down
        # Elgato never honored tile_fields -> fixed repo/branch fallback.
        primary_tokens, secondary_tokens = layout.resolve_tile_lines(
            self.config.view, ["repo"], ["branch"]
        )
        primary = layout.compose_line(s, primary_tokens)
        secondary = layout.compose_line(s, secondary_tokens)
        if key == self.selected():
            # Mark the first non-empty line so the act target stays identifiable
            # without turning an explicitly-empty line into a bare "* ".
            if primary:
                primary = f"* {primary}"
            elif secondary:
                secondary = f"* {secondary}"
        return TileView(
            ordinal,
            s.label,
            self._color(s),
            agent_type=s.agent_type,
            repo=primary,
            branch=secondary,
            status_text="OFFLINE" if down else s.status.value.upper(),
        )

    # --- render ---
    def render_all(self) -> dict[str, KeyRender]:
        out: dict[str, KeyRender] = {}
        for ordinal, iid in enumerate(self._slot_order):
            tile = self._slot_tile(ordinal)
            out[iid] = KeyRender(self._icons.render_tile_bytes(tile))
        for iid, kind in self._action_keys:
            out[iid] = KeyRender(self._icons.render_tile_bytes(self._action_tile(iid, kind)))
        return out

    def take_render_diff(self) -> dict[str, KeyRender]:
        current = self.render_all()
        diff: dict[str, KeyRender] = {}
        for iid, render in current.items():
            if self._last_bytes.get(iid) != render.image_png:
                diff[iid] = render
        self._last_bytes = {iid: r.image_png for iid, r in current.items()}
        return diff
