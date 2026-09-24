from __future__ import annotations

import hashlib
from dataclasses import dataclass

from . import layout
from .commands import Command, profile_for
from .config import Config
from .driver.base import PanelView, TileView
from .i18n import tr
from .model import AgentKey, AgentState, Status
from .project_icons import ProjectIconStore

_OPTION_LABEL_MAX = 14
# An armed destructive-action confirmation expires after this long, so a stale
# arm from minutes ago can never be completed by a later single press.
_CONFIRM_TTL_S = 5.0
# Ordering hysteresis: the fresh status-priority sort is adopted only after the
# target order has been stable this long, so tiles do not shuffle under the
# user's finger on every status flip.
_ORDER_SETTLE_S = 2.0
# Ignore a press on a slot whose occupant changed within this window — the
# press was almost certainly aimed at the previous occupant.
_SLOT_PRESS_GUARD_S = 0.3
# A preview opens after a 500 ms long-press. Keep the occupant-change guard
# alive through that gesture so it can never resolve the replacement tile.
_PREVIEW_SLOT_GUARD_S = _SLOT_PRESS_GUARD_S + 0.5
# The overview panel acknowledges a just-sent drill action this long, so
# returning to an unchanged amber tile doesn't read as "my press was lost".
_SENT_NOTE_TTL_S = 3.0
# How long a panel press holds the usage-limit detail (single-page decks).
_USAGE_DETAIL_HOLD_S = 6.0
# A drill / launcher / profile menu left untouched this long returns to the
# overview on its own: new blocks never yank an open view (see _on_new_block),
# so a forgotten drill would otherwise hide every later attention request.
MENU_IDLE_TIMEOUT_S = 60.0
# Bridge-vs-runtime clock skew tolerated before a bridge status_since_ms that
# lies in the future is ignored in favour of the local first-seen time.
_BRIDGE_SKEW_TOLERANCE_S = 5.0
SERVER_ACCENTS = ("teal", "violet", "orange", "pink", "lime")
_MANAGEMENT_ACTIONS = {"profiles", "new_agent"}
_APPROVE_ALWAYS_HINTS = ("always", "don't ask", "dont ask", "do not ask")
# Colour semantics for drill actions: the deck already teaches green=go,
# amber=caution, red=stop — the approve/deny press is the highest-stakes
# interaction, so it must not be a wall of identical blue tiles.
_ACTION_COLORS = {"approve": "green", "approve_always": "amber", "deny": "red"}
# Config section a management action's tile jumps to (klik-to-jump).
_MGMT_SECTION = {"profiles": "profiles", "new_agent": "start_profiles"}


def server_accent(server_id: str, accents: list[str] | None = None) -> str | None:
    palette = list(SERVER_ACCENTS) if accents is None else accents
    if not palette:
        return None
    digest = hashlib.sha1(server_id.encode()).digest()
    return palette[digest[0] % len(palette)]


def _looks_like_approve_always(label: str) -> bool:
    normalized = label.lower().replace("\u2019", "'")
    return any(hint in normalized for hint in _APPROVE_ALWAYS_HINTS)


def _looks_like_deny(label: str) -> bool:
    """A numbered option whose label starts with a bare 'No' is a denial (e.g.
    'No' / 'No, and tell Claude what to do differently')."""
    normalized = label.lower().strip()
    return normalized == "no" or normalized.startswith(("no,", "no "))


def option_action_id(option_key: str, option_label: str, profile) -> str | None:
    """The drill action a numbered prompt option stands for (approve /
    approve_always / deny), or None for an option that is none of those
    (e.g. an answer to a multiple-choice question)."""
    if _looks_like_approve_always(option_label):
        return "approve_always"
    if profile.approve and option_key == profile.approve[0]:
        return "approve"
    if profile.approve_always and option_key == profile.approve_always[0]:
        return "approve_always"
    if profile.deny and option_key == profile.deny[0]:
        return "deny"
    if _looks_like_deny(option_label):
        return "deny"
    return None


@dataclass(frozen=True)
class BinaryAnswer:
    """A blocked prompt that reduces to approve / deny: the option key to send
    for each, and ``sig`` — a fingerprint of the whole option list, so an answer
    prepared for one prompt is never applied to a different one."""

    approve: str
    deny: str
    sig: str


def binary_answer(detection: str, profile, safety) -> BinaryAnswer | None:
    """Approve/deny keys for a prompt that is a plain permission question, else None.

    Reuses the drill's option detection: every numbered option must map to an
    approve/approve_always/deny action (one unmapped option = a real question,
    not a yes/no), and both an approve and a deny option must exist. A prompt
    whose actions need an on-deck confirmation ([safety].require_confirm_for)
    is not binary here: a banner button cannot arm a confirmation. The y/n
    fallback the drill offers for unnumbered prompts is deliberately excluded —
    a banner never answers a prompt it could not parse.
    """
    options = layout.parse_options(detection or "")
    if len(options) < 2:
        return None
    ids = [option_action_id(o.key, o.label, profile) for o in options]
    if any(action is None for action in ids):
        return None
    if {"approve", "deny"} & set(safety.require_confirm_for):
        return None
    approve = next((o.key for o, a in zip(options, ids, strict=True) if a == "approve"), None)
    deny = next((o.key for o, a in zip(options, ids, strict=True) if a == "deny"), None)
    if approve is None or deny is None:
        return None
    digest = hashlib.sha1(
        "\n".join(f"{o.key}\t{o.label}" for o in options).encode()
    ).hexdigest()[:16]
    return BinaryAnswer(approve=approve, deny=deny, sig=digest)


@dataclass(frozen=True)
class IdleGroup:
    """Overview placeholder for the idle agents folded by [view].collapse_idle.

    Sits after the agents in the display list: ``expanded=False`` is the
    "+N idle" tile (press = show them), ``expanded=True`` the "hide idle" tile
    at the end of the unfolded list (press = fold them back)."""

    count: int
    expanded: bool = False


# Slot-tracking identity of the group tile (it has no AgentKey).
_IDLE_GROUP_SLOT = "idle-group"


@dataclass
class RenderState:
    tiles: list[TileView]
    panel: PanelView


class Orchestrator:
    def __init__(
        self,
        config: Config,
        slots: int | None = None,
        clock=None,
        project_icons: ProjectIconStore | None = None,
        wall_clock=None,
    ):
        import time

        self.config = config
        cols, rows = config.grid
        self.slots = slots if slots is not None else cols * rows
        self._clock = clock or time.monotonic
        # Wall clock (unix seconds): only used to convert the bridge's
        # status_since_ms into this orchestrator's (monotonic) clock basis.
        self._wall_clock = wall_clock or time.time
        self._project_icons = project_icons  # None -> the process-wide store
        self._agents: dict[AgentKey, AgentState] = {}
        # Status start time per agent, in the ``_clock`` basis:
        # (status, started at, the bridge's status_since_ms it came from or None).
        self._since: dict[AgentKey, tuple[Status, float, int | None]] = {}
        self._down: set[str] = set()
        self._down_since: dict[str, float] = {}  # server id -> when it went down
        # Servers seen connected at least once. A configured backend that never
        # came up (kept in config but not running, e.g. T3) is not announced
        # as offline on the panel; losing one that WAS up is a real outage.
        self._ever_up: set[str] = set()
        self._drill: AgentKey | None = None
        self.pins: dict[int, AgentKey] = {}
        self._drill_position = 0
        self._detection: str = ""
        self._page: int = 0
        self._phase: int = 0
        self._launcher: bool = False
        self._profile_menu: bool = False
        self._profile_menu_origin: str = "overview"
        # Last deck press (or view entry) — drives MENU_IDLE_TIMEOUT_S.
        self._last_press_at: float = self._clock()
        # When the current drill was opened: blocks that start later are
        # counted on the drill panel ("+2 more waiting").
        self._drill_since: float = 0.0
        # True while the open drill belongs to the triage loop (NEEDS YOU panel
        # press / desktop hotkey): answering it moves straight on to the next
        # longest-blocked agent instead of back to the overview.
        self._triage: bool = False
        # Blocked episodes already answered from a drill ({key: episode start}).
        # The agent stays BLOCKED until the bridge round-trip lands, so without
        # this the triage loop would hand the same prompt straight back.
        self._answered: dict[AgentKey, float] = {}
        # [view].collapse_idle: the idle agents are unfolded on the overview.
        self._idle_expanded: bool = False
        self._pending_confirm: tuple[str, AgentKey] | None = None
        self._pending_confirm_at: float = 0.0
        self._sent_note: tuple[str, float] | None = None  # (agent label, sent at)
        # Provider usage (CodexBar) shown on the calm overview panel; the host
        # app feeds it via set_usage from its UsagePoller.
        self._usage: list = []
        # (server_id | None,) while the deck shows a config error (set_config_error).
        self._config_error: tuple[str | None] | None = None
        self._usage_detail_until: float = 0.0
        self._usage_detail_page: int = 0
        # Ordering hysteresis state (see _ordered).
        self._display_order: list[AgentKey] = []
        self._placed_order: list[AgentKey | None] = []
        self._display_ranks: dict = {}
        self._target_keys: list[AgentKey] = []
        self._target_since: float = 0.0
        self._force_adopt: bool = False  # one-shot: adopt on next render, slot-guarded
        self._slot_changed_at: dict[int, float] = {}
        # Browser previews resolve against the last tile frame the driver
        # accepted, not against speculative state produced by a failed render.
        self._rendered_preview_slots: dict[int, AgentKey] = {}
        self._preview_slot_changed_at: dict[int, float] = {}

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

    def _bridge_start(self, since_ms: int | None) -> float | None:
        """The bridge's status start (unix ms) in the ``_clock`` basis, or None
        when there is none or it cannot be trusted.

        Bridge and runtime may run on different Macs: a start in the future by
        more than _BRIDGE_SKEW_TOLERANCE_S means the clocks disagree and the
        local first-seen time is used instead; a smaller skew clamps to "now"."""
        if since_ms is None:
            return None
        age = self._wall_clock() - since_ms / 1000.0
        if age < -_BRIDGE_SKEW_TOLERANCE_S:
            return None
        return self._clock() - max(0.0, age)

    def _touch(self, state: AgentState) -> bool:
        """Record when a pane entered its current status (for elapsed time).
        Returns True when this starts a NEW blocked episode.

        The bridge's ``status_since_ms`` (when present) is preferred over the
        moment this orchestrator first saw the status, so the elapsed time
        survives a runtime restart / source swap."""
        prev = self._since.get(state.key)
        bridge_ms = state.status_since_ms
        if prev is None or prev[0] is not state.status:
            # A status change always restarts the clock, even if a stale
            # bridge stamp (unchanged since the previous status) came along.
            fresh = prev is None or bridge_ms != prev[2]
            at = self._bridge_start(bridge_ms) if fresh else None
            self._since[state.key] = (
                state.status,
                self._clock() if at is None else at,
                bridge_ms,
            )
            return state.status is Status.BLOCKED
        if bridge_ms is not None and bridge_ms != prev[2]:
            # Same status, new bridge stamp: the bridge just started reporting
            # it (upgrade) or saw a flip this runtime missed. Adopt its time;
            # not announced as a new block (a bridge restart without its state
            # file would otherwise re-alert every blocked agent).
            at = self._bridge_start(bridge_ms)
            self._since[state.key] = (state.status, prev[1] if at is None else at, bridge_ms)
        return False

    def inherit_status_times(self, other: Orchestrator) -> None:
        """Adopt ``other``'s status start times (a source swap builds a fresh
        orchestrator; without this every local-fallback timer restarted at 0).
        Only valid when both share the same clock; limited to configured servers."""
        if other._clock is not self._clock:
            return
        allowed = {s.id for s in self.config.servers}
        for key, rec in other._since.items():
            if key.server_id in allowed:
                self._since.setdefault(key, rec)

    def _elapsed_text(self, key: AgentKey) -> str:
        rec = self._since.get(key)
        if rec is None:
            return ""
        return self._fmt_elapsed(self._clock() - rec[1])

    @staticmethod
    def _fmt_elapsed(seconds: float) -> str:
        s = int(max(0, seconds))
        if s < 60:
            # 5s buckets: the text is part of the baked tile's render signature,
            # so per-second granularity minted a fresh cache entry (full PIL
            # compose + PNG encode + disk write) nearly every tick during an
            # agent's whole first minute in a status.
            return f"{s - s % 5}s"
        if s < 3600:
            return f"{s // 60}m"
        return f"{s // 3600}h"

    # --- inbound state ---
    def apply_snapshot(self, server_id: str, states: list[AgentState]) -> None:
        previous = {key: state for key, state in self._agents.items() if key.server_id == server_id}
        drilled_before = (
            self._agents.get(self._drill)
            if self._drill is not None and self._drill.server_id == server_id
            else None
        )
        self._agents = {k: v for k, v in self._agents.items() if k.server_id != server_id}
        new_blocks = False
        for s in states:
            old = previous.get(s.key)
            if (
                old is not None
                and old.terminal_id
                and s.terminal_id
                and old.terminal_id != s.terminal_id
            ):
                self._since.pop(s.key, None)
            self._agents[s.key] = s
            new_blocks = self._touch(s) or new_blocks
        if new_blocks:
            self._on_new_block()
        live = set(self._agents)
        # Prune only this server's gone panes: another server's entries may be
        # inherited from a swapped-out orchestrator, awaiting its snapshot.
        self._since = {
            k: v for k, v in self._since.items() if k.server_id != server_id or k in live
        }
        if self._drill is not None and self._drill.server_id == server_id:
            if self._agents.get(self._drill) != drilled_before:
                self._pending_confirm = None
            current_drill = self._agents.get(self._drill)
            if (
                drilled_before is not None
                and current_drill is not None
                and drilled_before.terminal_id
                and current_drill.terminal_id
                and drilled_before.terminal_id != current_drill.terminal_id
            ):
                self._detection = ""

    def apply_event(self, server_id: str, state: AgentState) -> None:
        previous = self._agents.get(state.key)
        recycled = (
            previous is not None
            and previous.terminal_id
            and state.terminal_id
            and previous.terminal_id != state.terminal_id
        )
        if recycled:
            self._since.pop(state.key, None)
        if self._drill == state.key and previous != state:
            self._pending_confirm = None
            if recycled:
                self._detection = ""
        self._agents[state.key] = state
        if self._touch(state):
            self._on_new_block()

    def set_connection(self, server_id: str, up: bool) -> None:
        self._down.discard(server_id) if up else self._down.add(server_id)
        if up:
            self._ever_up.add(server_id)
            self._down_since.pop(server_id, None)
        else:
            self._down_since.setdefault(server_id, self._clock())
        if not up and self._pending_confirm is not None:
            # An armed confirmation must not survive an outage: the offline
            # drill hides it, so after a quick reconnect a single press could
            # complete a confirmation the user no longer sees as armed.
            if self._pending_confirm[1].server_id == server_id:
                self._pending_confirm = None

    def _server_count(self) -> int:
        """Servers the overview panel accounts for: the configured ones plus any
        unknown id reported down (so a stray id can never read as a partial)."""
        return len({s.id for s in self.config.servers} | self._down)

    def _down_for(self) -> str:
        """How long the longest current outage has lasted ("" = unknown)."""
        since = [self._down_since[s] for s in self._down if s in self._down_since]
        return self._fmt_elapsed(self._clock() - min(since)) if since else ""

    def _all_down(self) -> bool:
        """Every server is down -> the full OFFLINE panel. Some (not all) down
        is a partial outage: the calm/spotlight panel stays, with a note."""
        return bool(self._down) and len(self._down) >= self._server_count()

    def set_detection(self, text: str) -> None:
        if text != self._detection:
            self._pending_confirm = None
        self._detection = text

    def set_usage(self, data: list) -> None:
        """Latest ProviderUsage list from the host's UsagePoller ([] = none)."""
        self._usage = list(data)

    def set_config_error(self, active: bool, server_id: str | None = None) -> None:
        """Show the config-error screen instead of a fleet: the config file exists
        but cannot be loaded. ``server_id`` names the server whose bridge token is
        missing (None = any other load error). The deck never falls back to demo
        agents that would look like a healthy live fleet."""
        self._config_error = (server_id,) if active else None

    def _render_config_error(self) -> RenderState:
        (server_id,) = self._config_error
        reason = (
            self._tr("config_error_token", name=server_id)
            if server_id
            else self._tr("config_error_invalid")
        )
        tiles = [TileView(i, "", "empty") for i in range(self.slots)]
        color = self.config.theme.colors.get("offline", "red")
        panel = PanelView(
            self._tr("config_error_title"),
            [self._tr("config_error_hint")],
            color,
            headline=reason,
            solid=True,
            hint=self._tr("panel.recovers"),
        )
        return RenderState(tiles, panel)

    def consume_expired_panel_hold(self) -> bool:
        """True ONCE when a held usage detail just expired — the hold is gated
        at render time only, so without this an idle deck kept showing the
        \"6s\" detail until the next periodic full refresh (up to ~16s). Hosts
        call it each tick and issue a full render when it fires.

        Also True once when an idle drill/launcher/profile menu just timed out
        back to the overview (MENU_IDLE_TIMEOUT_S), for the same reason."""
        expired = False
        if self._usage_detail_until and self._clock() >= self._usage_detail_until:
            self._usage_detail_until = 0.0
            expired = True
        if self._expire_idle_view():
            expired = True
        return expired

    def _expire_idle_view(self) -> bool:
        """Leave an untouched drill/launcher/profile menu (or an unfolded idle
        list) for the plain overview."""
        if (
            self._drill is None
            and not self._launcher
            and not self._profile_menu
            and not self._idle_expanded
        ):
            return False
        if self._clock() - self._last_press_at < MENU_IDLE_TIMEOUT_S:
            return False
        self._drill = None
        self._triage = False
        self._idle_expanded = False
        self._launcher = False
        self._profile_menu = False
        self._profile_menu_origin = "overview"
        self._pending_confirm = None
        self._detection = ""
        self._page = 0  # land where fresh blocks sort (see _on_new_block)
        self._resettle()
        return True

    # --- drill helpers (used by app for read correlation) ---
    def drill_key(self) -> AgentKey | None:
        return self._drill

    def get_agent(self, key: AgentKey) -> AgentState | None:
        return self._agents.get(key)

    def agents(self) -> list[AgentState]:
        return list(self._agents.values())

    def status_elapsed(self, key: AgentKey) -> float | None:
        """Seconds the agent has been in its current status (None if unknown)."""
        rec = self._since.get(key)
        if rec is None:
            return None
        return max(0.0, self._clock() - rec[1])

    def answer_options(self, key: AgentKey, prompt: str) -> list[dict]:
        """The drill's answer choices for ``key``'s prompt, as plain data.

        Used by the desktop agent card, which shows the same choices as the deck
        drill (``_drill_layout``): numbered options parsed from the prompt
        (``kind="option"``, answered with ``[key, "enter"]``), else — once the
        prompt was actually read — the profile's approve/deny fallback
        (``kind="fallback"``); a T3 agent offers its backend actions
        (``kind="backend"``). ``confirm`` marks choices the deck would arm first.
        """
        agent = self._agents.get(key)
        if agent is None:
            return []
        safety = self.config.safety
        confirm_for = set(safety.require_confirm_for)
        out: list[dict] = []
        if agent.backend == "t3":
            for option in agent.backend_actions:
                if option["id"] == "approve_always" and not safety.approve_always:
                    continue
                out.append({
                    "key": option["id"],
                    "label": option["label"],
                    "id": option["id"],
                    "kind": "backend",
                    "confirm": bool(option.get("confirm")) or option["id"] in confirm_for,
                })
            return out
        if agent.status is not Status.BLOCKED or not prompt.strip():
            return []
        profile = profile_for(self.config, agent.agent_type)
        options = layout.parse_options(prompt)
        if options:
            for opt in options:
                action_id = self._option_action_id(opt.key, opt.label, profile)
                if action_id == "approve_always" and not safety.approve_always:
                    continue
                out.append({
                    "key": opt.key,
                    "label": opt.label,
                    "id": action_id,
                    "kind": "option",
                    "confirm": action_id in confirm_for,
                })
            return out
        fallback = ["approve"]
        if safety.approve_always:
            fallback.append("approve_always")
        fallback.append("deny")
        return [
            {
                "key": action_id,
                "label": self._tr(f"act.{action_id}"),
                "id": action_id,
                "kind": "fallback",
                "confirm": action_id in confirm_for,
            }
            for action_id in fallback
        ]

    def is_drill_pane(self, server_id: str, pane_id: str | None) -> bool:
        return (
            self._drill is not None
            and pane_id is not None
            and self._drill == AgentKey(server_id, pane_id)
        )

    def agent_for_preview(self, index: int) -> AgentState | None:
        """Resolve a browser tile to the agent it last rendered, read-only."""
        if index < 0 or index >= self.slots:
            return None
        key = self._rendered_preview_slots.get(index)
        if key is None:
            return None
        changed_at = self._preview_slot_changed_at.get(index, float("-inf"))
        if self._clock() - changed_at < _PREVIEW_SLOT_GUARD_S:
            return None
        return self._agents.get(key)

    def confirm_rendered_preview(self) -> None:
        """Commit the current tile-to-agent map after a successful render."""
        if self._profile_menu or self._launcher:
            rendered: dict[int, AgentKey] = {}
        elif self._drill is not None and self._drill in self._agents:
            rendered = {index: self._drill for index in range(self.slots)}
        else:
            ordered = self._place_pins([self._agents[key] for key in self._display_order if key in self._agents])
            shown, _ = layout.page(ordered, self._page, self._agent_slots())
            rendered = {index: agent.key for index, agent in enumerate(shown) if agent is not None}

        now = self._clock()
        for index in self._rendered_preview_slots.keys() | rendered.keys():
            if self._rendered_preview_slots.get(index) != rendered.get(index):
                self._preview_slot_changed_at[index] = now
        self._rendered_preview_slots = rendered

    def is_drilling(self) -> bool:
        return self._drill is not None

    def _on_new_block(self) -> None:
        """A new blocked episode needs attention: jump the overview to the top
        page and adopt the fresh sort NOW (blocked sorts first), instead of
        leaving the amber tile stranded on a page the user must hunt for. The
        adoption keeps the old display for slot-change tracking, so the press
        guard covers a finger already in flight. A drill/menu is never yanked —
        the overview re-sorts on exit anyway."""
        if self._drill is not None or self._launcher or self._profile_menu:
            return
        if self._page != 0:
            # The jump swaps every visible tile even when the ORDER is
            # unchanged (e.g. the blocker already sorted first), which the
            # display-diff guard cannot see — guard the whole first page.
            now = self._clock()
            for i in range(self._agent_slots()):
                self._slot_changed_at[i] = now
        self._page = 0
        self._force_adopt = True  # adopt on next render, WITH slot guards

    # --- render ---
    def _resettle(self) -> None:
        """Adopt the fresh sort on the next render — view transitions (paging,
        entering/leaving drill or menus) are safe moments to move tiles."""
        self._display_order = []
        self._placed_order = []
        self._slot_changed_at.clear()

    def _ordered(self) -> list[AgentState | None]:
        """Agents in DISPLAY order: the status-priority sort with hysteresis.

        A live re-sort on every event shuffles tiles under the user's finger —
        an agent unblocked from the phone mid-reach makes the press drill a
        different agent (and focus its pane on screen). Instead, tiles KEEP
        their positions while the fleet is changing; the fresh sort is adopted
        only once the target order has been stable for _ORDER_SETTLE_S, at
        view transitions (_resettle), or when a NEW BLOCK force-adopts
        (attention beats stability there — see _on_new_block; the slot press
        guard still protects a finger in flight). A STATUS-RANK change (idle ->
        working, blocked -> working after an approve, ...) also adopts
        immediately: that is a real transition the user is watching, not sort
        jitter — holding it parks a green tile among the idles for 2s. The
        hysteresis therefore damps only same-rank reshuffles. Removed agents
        drop out immediately; new agents append at the end until the next
        adoption."""
        target = layout.order_agents(
            self._overview_candidates(),
            self.config.overview_order,
            self.config.view.agent_order,
            blocked_since=self._blocked_since_map(),
        )
        target_keys = [s.key for s in target]
        now = self._clock()
        if target_keys != self._target_keys:
            self._target_keys = target_keys
            self._target_since = now
        by_key = {s.key: s for s in target}
        ranks = {s.key: layout.status_rank(s.status) for s in target}
        rank_changed = any(ranks[k] != r for k, r in self._display_ranks.items() if k in ranks)
        if (
            not self._display_order
            or self._force_adopt
            or rank_changed
            or now - self._target_since >= _ORDER_SETTLE_S
        ):
            display = list(target_keys)
            self._force_adopt = False
            self._display_ranks = ranks
        else:
            display = [k for k in self._display_order if k in by_key]
            display += [k for k in target_keys if k not in display]
        self._display_order = display
        placed = self._place_pins([by_key[k] for k in display])
        group = self._idle_group()
        if group is not None:
            placed = [*placed, group]
        keys = [
            _IDLE_GROUP_SLOT if isinstance(agent, IdleGroup) else agent.key if agent else None
            for agent in placed
        ]
        for i, key in enumerate(keys):
            if i < len(self._placed_order) and self._placed_order[i] != key:
                self._slot_changed_at[i] = now
        self._placed_order = keys
        return placed

    def _collapsed_idle(self) -> list[AgentKey]:
        """Idle agents folded into the group tile (collapse_idle on, the list
        not unfolded). Pinned agents keep their tile and are never folded."""
        if not self.config.view.collapse_idle:
            return []
        pinned = set(self.pins.values())
        return [
            s.key
            for s in self._agents.values()
            if s.lifecycle == "active" and s.status is Status.IDLE and s.key not in pinned
        ]

    def _overview_candidates(self) -> list[AgentState]:
        """Agents that get their own overview tile."""
        folded = set() if self._idle_expanded else set(self._collapsed_idle())
        return [
            s for s in self._agents.values() if s.lifecycle == "active" and s.key not in folded
        ]

    def _idle_group(self) -> IdleGroup | None:
        count = len(self._collapsed_idle())
        if not count:
            return None
        return IdleGroup(count, expanded=self._idle_expanded)

    def _toggle_idle_group(self) -> None:
        """Unfold (or fold back) the idle agents and land on the page that
        shows the result: the first idle agent, or the "+N idle" tile."""
        self._idle_expanded = not self._idle_expanded
        self._resettle()
        ordered = self._ordered()
        target = next(
            (
                pos
                for pos, entry in enumerate(ordered)
                if (
                    isinstance(entry, IdleGroup)
                    if not self._idle_expanded
                    else entry is not None
                    and not isinstance(entry, IdleGroup)
                    and entry.status is Status.IDLE
                    and entry.key not in self.pins.values()
                )
            ),
            0,
        )
        self._page = target // self._agent_slots()

    def _place_pins(self, ordered):
        """Absolute overview positions, including holes for temporarily absent agents.

        Pin placement is shared by rendering, presses, animation and previews.
        Status sorting may only fill unreserved positions.
        """
        if not self.pins:
            return ordered
        pinned = set(self.pins.values())
        rest = iter(agent for agent in ordered if agent.key not in pinned)
        size = max(sum(agent.key not in pinned for agent in ordered) + len(pinned), max(self.pins) + 1)
        return [self._agents.get(self.pins[i]) if i in self.pins else next(rest, None) for i in range(size)]

    def toggle_pin(self, key, position):
        old = next((i for i, value in self.pins.items() if value == key), None)
        if old is not None:
            del self.pins[old]
        else:
            self.pins[position] = key
        self._resettle()

    def _agent_color(self, s: AgentState) -> str:
        if s.key.server_id in self._down:
            return self.config.theme.colors.get("offline", "red")
        if s.attention == "error":
            return "red"
        if s.lifecycle != "active":
            return "grey"
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
            "profiles": self._tr("profiles_entry"),
            "notifications": self._tr("mgmt.notifications"),
            "safety": self._tr("mgmt.safety"),
            "theme": self._tr("mgmt.theme"),
            "new_agent": self._tr("new_agent"),
        }.get(action, action)

    def _note_sent(self, key: AgentKey) -> None:
        agent = self._agents.get(key)
        if agent is not None:
            self._sent_note = (agent.label, self._clock())

    def _blocked_queue(self) -> list[AgentKey]:
        """Active BLOCKED agents, longest-waiting first (the triage order).

        The spotlight names the head of this queue and a spotlight press opens
        it, so both must come from the same ordering."""

        def started(s):
            rec = self._since.get(s.key)
            return rec[1] if rec else 0.0

        blocked = [
            s
            for s in self._agents.values()
            if s.status is Status.BLOCKED and s.lifecycle == "active"
        ]
        return [s.key for s in sorted(blocked, key=lambda s: (started(s), s.key.server_id, s.key.pane_id))]

    def _triage_queue(self) -> list[AgentKey]:
        """The blocked queue minus episodes already answered from a drill."""
        live = {}
        for key, at in self._answered.items():
            rec = self._since.get(key)
            if rec is not None and rec[0] is Status.BLOCKED and rec[1] == at:
                live[key] = at
        self._answered = live  # a new status (or episode) forgets the answer
        return [key for key in self._blocked_queue() if key not in live]

    def _note_answered(self, key: AgentKey) -> None:
        rec = self._since.get(key)
        if rec is not None and rec[0] is Status.BLOCKED:
            self._answered[key] = rec[1]

    def _blocked_since_map(self) -> dict[AgentKey, float]:
        """When each currently BLOCKED agent entered BLOCKED (overview sort)."""
        return {
            key: at for key, (status, at, _ms) in self._since.items() if status is Status.BLOCKED
        }

    def _blocked_spotlight(self) -> layout.Spotlight | None:
        """The longest-waiting BLOCKED agent (and who waits after it), or None."""
        queue = self._blocked_queue()
        if not queue:
            return None
        oldest = self._agents[queue[0]]
        detail = " · ".join(part for part in (oldest.tab, oldest.agent_type) if part)
        others = tuple(
            (self._agents[key].label, self._elapsed_text(key)) for key in queue[1:3]
        )
        return layout.Spotlight(oldest.label, self._elapsed_text(oldest.key), detail, others)

    def render(self) -> RenderState:
        if self._config_error is not None:
            return self._render_config_error()
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
        # Keep the legacy visibility switch; explicit repo tokens still show the repository.
        fb_primary = ["project"] if "repo" in fields else []
        # The compact second line identifies the Herdr tab first; branch is
        # useful context but may be ellipsized when the physical tile is tight.
        fb_secondary = [token for token in ("tab", "branch") if token in fields]
        primary_tokens, secondary_tokens = layout.resolve_tile_lines(
            self.config.view, fb_primary, fb_secondary
        )
        show_server_tags = "server" in fields
        management = self._management_indices()
        management_mode = self.config.view.management == "bottom_row"
        tiles: list[TileView] = []
        for i in range(self.slots):
            if i in management:
                tiles.append(
                    TileView(
                        i,
                        self._management_label(management[i]),
                        "grey",
                        section=_MGMT_SECTION.get(management[i]),
                    )
                )
            elif not management_mode and i == self.slots - 1:  # reserved launcher tile
                # "launcher" renders dark with a green label — full status-green
                # here read as one more WORKING agent under solid fill.
                tiles.append(
                    TileView(i, self._tr("new_agent"), "launcher", section="start_profiles")
                )
            elif i < len(shown) and isinstance(shown[i], IdleGroup):
                group = shown[i]
                if group.expanded:
                    tiles.append(
                        TileView(
                            i,
                            self._tr("idle_group_hide"),
                            "grey",
                            subtext=self._tr("idle_group_hide_sub"),
                            section="view",
                        )
                    )
                else:
                    tiles.append(
                        TileView(
                            i,
                            self._tr("idle_group", n=group.count),
                            "grey",
                            subtext=self._tr("idle_group_show"),
                            section="view",
                        )
                    )
            elif i < len(shown) and shown[i] is not None:
                s = shown[i]
                phase = self._phase if s.status is Status.WORKING else None
                down = s.key.server_id in self._down
                tag = ("T3" if s.backend == "t3" else "HERDR") if show_server_tags else None
                # A T3 thread takes the place of a terminal tab. Give its title
                # the whole secondary line by default; explicit layouts still win.
                agent_secondary = secondary_tokens
                if s.backend == "t3" and self.config.view.tile_secondary is None:
                    agent_secondary = ["tab"] if "tab" in fields else []
                elif (
                    s.backend == "herdr"
                    and self.config.view.tile_secondary is None
                    and "title" in fields
                    and s.title
                ):
                    agent_secondary = ["title"]
                primary, secondary = layout.compose_tile_lines(s, primary_tokens, agent_secondary)
                accent = (
                    server_accent(s.key.server_id, self.config.theme.server_accents)
                    if show_server_tags
                    else None
                )
                tiles.append(
                    layout.agent_tile_view(
                        i,
                        s,
                        self.config.view,
                        color=self._agent_color(s),
                        spinner=phase,
                        repo=primary,
                        branch=secondary,
                        status_text=layout.tile_status_text(
                            s, self.config.view.language, down
                        )
                        if "status" in fields
                        else None,
                        time_text=self._elapsed_text(s.key) if "time" in fields else None,
                        pinned=s.key in self.pins.values(),
                        server_tag=tag,
                        server_accent=accent,
                        section="view",
                        project_icons=self._project_icons,
                    )
                )
            elif (self._page % pages) * agent_slots + i in self.pins:
                key = self.pins[(self._page % pages) * agent_slots + i]
                label = "pinned_absent" if key.server_id in self._down else "pinned_missing"
                tiles.append(TileView(i, self._tr(label), "grey", subtext=self._tr("unpin")))
            else:
                tiles.append(TileView(i, "", "empty"))
        spotlight = self._blocked_spotlight()
        if (
            self._usage
            and not self._all_down()
            and spotlight is None
            and self._clock() < self._usage_detail_until
        ):
            # Held usage detail (panel press on a single-page deck): every
            # provider window with its reset time, in place of the overview;
            # repeated presses page when there are more windows than lines.
            # A full outage or a blocked agent takes the panel back at once; a
            # partial outage does not (the user still wants their limits).
            detail_pages = layout.usage_detail_pages(self._usage)
            page = min(self._usage_detail_page, detail_pages - 1)
            panel = PanelView(
                self._tr("usage_title"),
                gauges=layout.usage_detail_gauges(
                    self._usage,
                    page=self._usage_detail_page,
                    lang=self.config.view.language,
                ),
                meta=self._tr("usage_meta"),
                page=(page, detail_pages) if detail_pages > 1 else None,
                hint=self._tr(
                    "panel.press_more" if page + 1 < detail_pages else "panel.press_close"
                ),
            )
            return RenderState(tiles, panel)
        sent = ""
        if self._sent_note is not None:
            label, at = self._sent_note
            if self._clock() - at <= _SENT_NOTE_TTL_S:
                # acknowledge the action while its status change propagates —
                # the tile stays amber until the bridge round-trip completes
                sent = self._tr("sent", label=label)
            else:
                self._sent_note = None
        all_down = self._all_down()
        panel = layout.panel_overview(
            layout.summary(self._agents.values()),
            self._page % pages,
            pages,
            # A full outage shows OFFLINE whatever connected before; in a
            # partial one only servers that were up once get the note.
            self._down if all_down else self._down & self._ever_up,
            sum(s.lifecycle == "active" for s in self._agents.values()),
            spotlight,
            lang=self.config.view.language,
            usage_gauges=(
                layout.usage_summary_gauges(
                    self._usage,
                    lang=self.config.view.language,
                )
                if self._usage
                else None
            ),
            servers=self._server_count(),
            colors=self.config.theme.colors,
            sent=sent,
            down_for=self._down_for() if all_down else "",
            left=len(self._triage_queue()) if sent else None,
        )
        return RenderState(tiles, panel)

    def _render_profile_menu(self) -> RenderState:
        names = list(self.config.meta.profile_names)
        back_i = self.slots - 1
        tiles: list[TileView] = []
        for i in range(self.slots):
            if i < len(names) and i < back_i:
                name = names[i]
                label = f"* {name}" if name == self.config.meta.active_profile else name
                tiles.append(TileView(i, label[:_OPTION_LABEL_MAX], "blue", section="profiles"))
            elif i == back_i:
                tiles.append(TileView(i, self._tr("back"), "grey"))
            else:
                tiles.append(TileView(i, "", "empty"))
        locked = (
            self._tr("locked_by_env")
            if self.config.meta.env_locked_profile
            else self._tr("pick_profile")
        )
        return RenderState(tiles, PanelView(self._tr("profiles_title"), headline=locked))

    def _render_launcher(self) -> RenderState:
        types = list(self.config.start_profiles)
        entries = types + (["Profiles"] if len(self.config.meta.profile_names) > 1 else [])
        back_i = self.slots - 1
        tiles: list[TileView] = []
        for i in range(self.slots):
            if i < len(entries) and i < back_i:
                entry = entries[i]
                agent_type = entry if entry in self.config.start_profiles else None
                # "Profiles" is a logic sentinel matched by _press_launcher — translate
                # only the rendered label, never the entries list.
                label = self._tr("profiles_entry") if agent_type is None else entry
                tiles.append(
                    TileView(
                        i,
                        label,
                        "blue",
                        agent_type=agent_type,
                        section=("start_profiles" if agent_type else "profiles"),
                    )
                )
            elif i == back_i:
                tiles.append(TileView(i, self._tr("back"), "grey"))
            else:
                tiles.append(TileView(i, "", "empty"))
        lines = []
        target = self._launch_server()
        if target is not None and len(self.config.overview_order) > 1:
            # With several servers the new agent's destination is not obvious:
            # say where it will start instead of silently using the first one.
            lines.append(self._tr("launch_on", server=target))
        panel = PanelView(self._tr("new_agent_title"), lines, headline=self._tr("pick_type"))
        return RenderState(tiles, panel)

    def _launch_server(self) -> str | None:
        """Where a launcher press starts the agent: the first overview server."""
        return self.config.overview_order[0] if self.config.overview_order else None

    def tick(self) -> list[int]:
        """Advance the spinner phase; return overview tile indices that are working.

        Uses a READ-ONLY view of the current display order: order adoption is a
        visible change, so it may only happen where a frame is produced
        (render) or a press is being resolved (slot-guarded) — an idle tick
        that skips rendering must not silently move the order presses resolve
        against."""
        if self._drill is not None or self._launcher or self._profile_menu:
            return []
        self._phase += 1
        by_key = {s.key: s for s in self._agents.values()}
        display = [k for k in self._display_order if k in by_key]
        if not display:
            display = [
                s.key
                for s in layout.order_agents(
                    self._overview_candidates(),
                    self.config.overview_order,
                    self.config.view.agent_order,
                    blocked_since=self._blocked_since_map(),
                )
            ]
        shown, _ = layout.page(self._place_pins([by_key[k] for k in display]), self._page, self._agent_slots())
        return [i for i, s in enumerate(shown) if s is not None and s.status is Status.WORKING]

    def _drill_layout(self) -> tuple[list, int, int]:
        """Drill action tiles plus the fixed Stop/Back indices.

        Blocked agent -> parsed prompt options (send the number). Otherwise ->
        configured quick-send macros (send text). Each action is a dict with a
        ``label`` and a callable ``make`` that builds the Command for a key.
        """
        agent = self._agents.get(self._drill)
        stop_i, back_i = self.slots - 2, self.slots - 1
        actions: list[dict] = []
        if agent is not None and agent.backend == "t3":
            for option in agent.backend_actions:
                if option["id"] == "approve_always" and not self.config.safety.approve_always:
                    continue
                actions.append({"id": option["id"], "label": option["label"], "confirm": option.get("confirm", False),
                    "subtext": option.get("subtext", ""),
                    "confirm_key": agent.backend_revision + str(option["payload"]),
                    "make": lambda key, o=option, rev=agent.backend_revision: Command(
                        "backend_action", key.server_id, key.pane_id, action=o["id"],
                        payload=o["payload"], decision_revision=rev)})
            if "continue" in agent.capabilities:
                for macro in self.config.macros:
                    actions.append({"label": macro.label[:_OPTION_LABEL_MAX],
                        "make": lambda key, text=macro.text, rev=agent.backend_revision: Command(
                            "backend_action", key.server_id, key.pane_id, action="continue",
                            text=text, decision_revision=rev)})
            return actions[:max(0, stop_i - 1)], stop_i, back_i
        if agent is not None and agent.status is Status.BLOCKED:
            options = layout.parse_options(self._detection)
            if options:
                profile = self._profile_for(self._drill)
                for opt in options:
                    action_id = self._option_action_id(opt.key, opt.label, profile)
                    if action_id == "approve_always" and not self.config.safety.approve_always:
                        continue
                    actions.append(
                        {
                            "id": action_id,
                            # Confirmation identity: two options can share an id
                            # (e.g. two "No…" deny variants) but must arm and
                            # confirm independently.
                            "confirm_key": f"opt:{opt.key}",
                            "label": opt.key,
                            "subtext": opt.label,
                            # A numbered menu selects on the digit but only submits on
                            # Enter (the digit alone just moves the cursor), so send
                            # both — matching the profiles' "<digit>, enter" approve keys.
                            "make": (
                                lambda key, k=opt.key, terminal_id=agent.terminal_id: Command(
                                    "act_if_blocked",
                                    key.server_id,
                                    key.pane_id,
                                    keys=[k, "enter"],
                                    terminal_id=terminal_id or None,
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
                fallback = [("approve", self._tr("act.approve"), profile.approve)]
                if self.config.safety.approve_always:
                    fallback.append(
                        ("approve_always", self._tr("act.approve_always"), profile.approve_always)
                    )
                fallback.append(("deny", self._tr("act.deny"), profile.deny))
                for action_id, label, keys in fallback:
                    actions.append(
                        {
                            "id": action_id,
                            "confirm_key": f"fb:{action_id}",
                            "label": label,
                            "make": (
                                lambda key, ks=keys, terminal_id=agent.terminal_id: Command(
                                    "act_if_blocked",
                                    key.server_id,
                                    key.pane_id,
                                    keys=ks,
                                    terminal_id=terminal_id or None,
                                )
                            ),
                        }
                    )
        elif agent is not None:
            if "refresh_title" in agent.capabilities:
                actions.append(
                    {
                        "id": "refresh_title",
                        "label": self._tr("refresh_title"),
                        "make": (
                            lambda key, terminal_id=agent.terminal_id: Command(
                                "refresh_title",
                                key.server_id,
                                key.pane_id,
                                terminal_id=terminal_id or None,
                            )
                        ),
                    }
                )
            for m in self.config.macros:
                actions.append(
                    {
                        "label": m.label[:_OPTION_LABEL_MAX],
                        "make": (
                            lambda key, t=m.text, terminal_id=agent.terminal_id: Command(
                                "send_text",
                                key.server_id,
                                key.pane_id,
                                text=t,
                                terminal_id=terminal_id or None,
                            )
                        ),
                    }
                )
        return actions[:max(0, stop_i - 1)], stop_i, back_i

    def _armed_action(self) -> str | None:
        """The armed (fresh, for the drilled agent) confirm action id, or None."""
        if self._drill is None or self._pending_confirm is None:
            return None
        action, key = self._pending_confirm
        if key != self._drill or self._clock() - self._pending_confirm_at > _CONFIRM_TTL_S:
            return None
        return action

    def _arm_confirm(self, action: str, key: AgentKey) -> None:
        self._pending_confirm = (action, key)
        self._pending_confirm_at = self._clock()

    def _confirm_armed(self, action: str, key: AgentKey) -> bool:
        return (
            self._pending_confirm == (action, key)
            and self._clock() - self._pending_confirm_at <= _CONFIRM_TTL_S
        )

    def _drill_down(self) -> bool:
        """Is the drilled agent's server currently disconnected?"""
        return self._drill is not None and self._drill.server_id in self._down

    def _tr(self, key: str, **fmt: object) -> str:
        return tr(self.config.view.language, key, **fmt)

    def _render_drill(self) -> RenderState:
        agent = self._agents.get(self._drill)
        actions, stop_i, back_i = self._drill_layout()
        # The overview marks a down server everywhere; the drill must too —
        # otherwise the option tiles stay colourful and pressable while the
        # dead connector silently drops every command.
        down = self._drill_down()
        # An armed confirmation must be visible: without feedback the first press
        # looks like a dead button and the natural "retry" press completes a
        # confirmation the user never knew was armed.
        armed = self._armed_action() if not down else None
        tiles: list[TileView] = []
        for i in range(self.slots):
            if i < len(actions):
                label = actions[i]["label"]
                if armed is not None and actions[i].get("confirm_key") == armed:
                    label = self._tr("sure")
                color = "grey" if down else _ACTION_COLORS.get(actions[i].get("id"), "blue")
                tiles.append(
                    TileView(
                        i,
                        label,
                        color,
                        subtext=actions[i].get("subtext"),
                        section="answer_profiles",
                    )
                )
            elif i == stop_i - 1 and agent is not None:
                label = self._tr("unpin") if agent.key in self.pins.values() else self._tr("pin")
                tiles.append(TileView(i, label, "grey"))
            elif i == stop_i:
                stop_label = self._tr("sure") if armed == "act_force" else self._tr("stop")
                unavailable = agent is not None and agent.backend == "t3" and "stop" not in agent.capabilities
                tiles.append(
                    TileView(i, stop_label, "grey" if down or unavailable else "red", section="answer_profiles")
                )
            elif i == back_i:
                tiles.append(TileView(i, self._tr("back"), "grey"))
            else:
                tiles.append(TileView(i, "", "empty"))
        panel = (
            layout.panel_detail(
                agent,
                agent.preview if agent.backend == "t3" else self._detection,
                lang=self.config.view.language,
                elapsed=self._elapsed_text(agent.key),
                color=self._agent_color(agent),
            )
            if agent is not None
            else PanelView("")
        )
        if down:
            panel = PanelView(
                self._tr("offline_title"),
                color=self.config.theme.colors.get("offline", "red"),
                headline=self._tr("reconnecting"),
                meta=panel.meta,
            )
        elif armed is not None and agent is not None:
            panel.title = self._tr("press_to_confirm")
            panel.color = self.config.theme.colors.get("offline", "red")
            panel.hint = ""
        elif agent is not None:
            others = self._blocked_since_drill()
            if others:
                panel.aside = self._tr("others_blocked", n=others)
            if self._triage and self._sent_note is not None:
                # The triage loop moved straight on to the next agent: confirm
                # the previous answer went out, as the overview would have.
                label, at = self._sent_note
                if self._clock() - at <= _SENT_NOTE_TTL_S and agent.label != label:
                    panel.sent = self._tr("sent", label=label)
        return RenderState(tiles, panel)

    def _blocked_since_drill(self) -> int:
        """Other agents whose blocked episode started after this drill opened —
        they cannot yank the drill, so the panel must at least say so."""
        count = 0
        for key, state in self._agents.items():
            if key == self._drill or state.status is not Status.BLOCKED:
                continue
            if state.lifecycle != "active":
                continue
            since = self._since.get(key)
            if since is not None and since[1] > self._drill_since:
                count += 1
        return count

    # --- presses ---
    def _profile_for(self, key: AgentKey):
        return profile_for(self.config, self._agents[key].agent_type)

    def _option_action_id(self, option_key: str, option_label: str, profile) -> str | None:
        return option_action_id(option_key, option_label, profile)

    def on_press(self, index: int) -> list[Command]:
        self._last_press_at = self._clock()
        if self._profile_menu:
            return self._press_profile_menu(index)
        if self._launcher:
            return self._press_launcher(index)
        if self._drill is not None:
            return self._press_drill(index)
        return self._press_overview(index)

    def _press_overview(self, index: int) -> list[Command]:
        if index in self._panel_indices():
            if not self._all_down() and self._blocked_queue():
                # The panel shows the NEEDS YOU spotlight: its press starts the
                # triage loop at the agent that has waited longest. (Agents just
                # answered are skipped; if that is all of them the press waits
                # for the bridge rather than paging under the spotlight.)
                self._usage_detail_until = 0.0
                return self.triage()
            _, pages = layout.page(self._ordered(), self._page, self._agent_slots())
            detail_can_show = (
                self._usage and not self._all_down() and self._blocked_spotlight() is None
            )
            if pages == 1 and detail_can_show:
                # Nothing to page through: the press shows a held usage detail
                # instead (reset times per provider window); repeated presses
                # page through windows beyond the 3-line body, then hide.
                # While a spotlight/offline panel owns the view the press does
                # NOT arm an invisible timer that would pop up later.
                now = self._clock()
                if now >= self._usage_detail_until:
                    self._usage_detail_page = 0
                    self._usage_detail_until = now + _USAGE_DETAIL_HOLD_S
                elif self._usage_detail_page + 1 < layout.usage_detail_pages(self._usage):
                    self._usage_detail_page += 1
                    self._usage_detail_until = now + _USAGE_DETAIL_HOLD_S
                else:
                    self._usage_detail_until = 0.0
                return []
            self._page += 1
            self._usage_detail_until = 0.0
            self._resettle()  # a page flip is a safe moment to adopt the fresh sort
            return []
        # Any non-panel press moves attention elsewhere — a held usage detail
        # must not outlive it (it would hide e.g. the "sent ›" acknowledgement
        # after a drill action).
        self._usage_detail_until = 0.0
        management = self._management_indices()
        if index in management:
            action = management[index]
            if action == "profiles":
                self._profile_menu = True
                self._profile_menu_origin = "overview"
            elif action == "new_agent":
                self._launcher = True
            self._pending_confirm = None
            self._resettle()
            return []
        if self.config.view.management != "bottom_row" and index == self.slots - 1:
            self._launcher = True
            self._pending_confirm = None
            self._resettle()
            return []
        agent_slots = self._agent_slots()
        ordered = self._ordered()
        shown, pages = layout.page(ordered, self._page, agent_slots)
        if 0 <= index < len(shown):
            # The occupant of this slot changed a moment ago — the press was
            # almost certainly aimed at the previous occupant. Swallow it; the
            # user sees the new tile and can press again deliberately.
            pos = (self._page % pages) * agent_slots + index
            if self._clock() - self._slot_changed_at.get(pos, float("-inf")) < _SLOT_PRESS_GUARD_S:
                return []
            selected = shown[index]
            if isinstance(selected, IdleGroup):
                self._toggle_idle_group()
                return []
            if selected is None:
                if pos in self.pins:
                    key = self.pins[pos]
                    return [Command("toggle_pin", key.server_id, key.pane_id, payload={"position": pos})]
                return []
            self._triage = False
            return self._open_drill(selected, pos)
        return []

    def _open_drill(self, selected: AgentState, pos: int) -> list[Command]:
        """Enter the drill view for ``selected`` (overview position ``pos``)."""
        self._drill_position = pos
        key = selected.key
        self._drill = key
        self._drill_since = self._clock()
        self._detection = ""
        self._pending_confirm = None
        self._resettle()  # returning from drill re-sorts anyway
        if selected.backend == "t3":
            self._detection = selected.preview
            return [Command("read", key.server_id, key.pane_id)]
        # Focus the agent in the on-screen herdr session AND read its prompt.
        return [
            Command(
                "focus",
                key.server_id,
                key.pane_id,
                terminal_id=selected.terminal_id or None,
            ),
            Command(
                "read",
                key.server_id,
                key.pane_id,
                source="detection",
                terminal_id=selected.terminal_id or None,
            ),
        ]

    def _overview_position(self, key: AgentKey) -> int:
        """Where ``key`` sits in the overview (pin position for its drill)."""
        for pos, agent in enumerate(self._ordered()):
            if isinstance(agent, AgentState) and agent.key == key:
                return pos
        return 0

    def triage(self) -> list[Command]:
        """Open the drill of the agent that has been blocked longest.

        Entry point of the triage loop — the NEEDS YOU panel press and the
        desktop's "next blocked agent" hotkey. Repeating it while a triage
        drill is open jumps to the next blocked agent (wrapping around).
        Answering inside a triage drill continues with the next one (see
        _press_drill). No blocked agent -> nothing changes."""
        self._last_press_at = self._clock()
        queue = self._triage_queue()
        if not queue:
            return []
        target = queue[0]
        if self._drill is not None and self._drill in queue and len(queue) > 1:
            target = queue[(queue.index(self._drill) + 1) % len(queue)]
        self._launcher = False
        self._profile_menu = False
        self._profile_menu_origin = "overview"
        self._usage_detail_until = 0.0
        self._triage = True
        self._drill = None  # position lookup below must read the overview order
        return self._open_drill(self._agents[target], self._overview_position(target))

    def open_agent(self, key: AgentKey) -> list[Command] | None:
        """Open ``key``'s drill from outside the deck (a notification banner
        click). Leaves any menu/triage first, like the triage entry point.
        None when the agent is unknown (it vanished since the banner)."""
        agent = self._agents.get(key)
        if agent is None:
            return None
        self._last_press_at = self._clock()
        self._launcher = False
        self._profile_menu = False
        self._profile_menu_origin = "overview"
        self._usage_detail_until = 0.0
        self._triage = False
        self._drill = None  # position lookup below must read the overview order
        return self._open_drill(agent, self._overview_position(key))

    def note_external_answer(self, key: AgentKey) -> None:
        """The user answered ``key`` outside the deck (a banner button/reply):
        acknowledge it like a drill action and close its now-stale drill."""
        self._note_sent(key)
        self._note_answered(key)
        if self._drill == key:
            self._drill = None
            self._triage = False
            self._pending_confirm = None
            self._resettle()

    def _after_drill_action(self, acted: AgentKey) -> list[Command]:
        """Leave a drill after an action: in triage, straight into the next
        longest-blocked agent's drill; otherwise back to the overview. The
        agent just answered is still BLOCKED until the bridge round-trip
        lands, so it is skipped explicitly."""
        self._drill = None
        self._note_answered(acted)
        if self._triage:
            queue = self._triage_queue()
            if queue:
                return self._open_drill(self._agents[queue[0]], self._overview_position(queue[0]))
        self._triage = False
        self._resettle()
        return []

    def _press_launcher(self, index: int) -> list[Command]:
        types = list(self.config.start_profiles)
        entries = types + (["Profiles"] if len(self.config.meta.profile_names) > 1 else [])
        back_i = self.slots - 1
        if index == back_i:
            self._launcher = False
            self._resettle()
            return []
        if index < len(entries) and index < back_i:
            name = entries[index]
            if name == "Profiles":
                self._profile_menu = True
                self._profile_menu_origin = "launcher"
                self._launcher = False
                return []
            argv = list(self.config.start_profiles[name])
            server = self._launch_server()
            if server is None:
                return []
            self._launcher = False  # return to overview
            return [Command("start", server, text=name, keys=argv)]
        return []

    def _press_profile_menu(self, index: int) -> list[Command]:
        names = list(self.config.meta.profile_names)
        back_i = self.slots - 1
        if index == back_i:
            self._profile_menu = False
            self._launcher = self._profile_menu_origin == "launcher"
            self._resettle()
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
            self._triage = False
            self._pending_confirm = None
            self._resettle()
            return []
        if key not in self._agents:
            self._drill = None
            self._triage = False
            self._pending_confirm = None
            self._resettle()
            return []
        if index == stop_i - 1:
            return [Command("toggle_pin", key.server_id, key.pane_id, payload={"position": self._drill_position})]
        if self._drill_down():
            # The connector is down: a command would be dropped silently. Only
            # Back (handled above) works until the server reconnects.
            return []
        if index == stop_i:  # Stop — always, unconditional
            target = self._agents[key]
            if target.backend == "t3" and "stop" not in target.capabilities:
                return []
            action = "act_force"
            if action in self.config.safety.require_confirm_for and not self._confirm_armed(
                action, key
            ):
                self._arm_confirm(action, key)  # (re-)arm; an expired arm never fires
                return []
            self._pending_confirm = None
            target = self._agents[key]
            cmd = Command(
                "act_force",
                key.server_id,
                key.pane_id,
                keys=self._profile_for(key).stop,
                terminal_id=target.terminal_id or None,
            )
            if target.backend == "t3":
                cmd = Command("backend_action", key.server_id, key.pane_id,
                              action="stop", decision_revision=target.backend_revision)
            self._note_sent(key)
            return [cmd, *self._after_drill_action(key)]
        if index < len(actions):  # send option number or macro text
            action_id = actions[index].get("id")
            confirm_key = actions[index].get("confirm_key") or f"idx:{index}"
            if (actions[index].get("confirm") or action_id in self.config.safety.require_confirm_for) and not self._confirm_armed(
                confirm_key, key
            ):
                self._arm_confirm(confirm_key, key)  # (re-)arm; an expired arm never fires
                return []
            self._pending_confirm = None
            cmd = actions[index]["make"](key)
            self._note_sent(key)
            return [cmd, *self._after_drill_action(key)]
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
        self._ever_up &= allowed_servers
        self._launcher = False
        self._profile_menu = False
        self._profile_menu_origin = "overview"
        self._drill = None
        self._triage = False
        self._idle_expanded = False
        self._detection = ""
        self._page = 0
        self._pending_confirm = None
        self._resettle()

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
            self._triage = False
            self._detection = ""
            self._pending_confirm = None
        self._resettle()
