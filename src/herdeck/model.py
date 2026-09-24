from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlsplit


class Status(str, Enum):
    WORKING = "working"
    IDLE = "idle"
    BLOCKED = "blocked"
    DONE = "done"
    # Herdeck-side derived state: the agent itself is done/idle but an external
    # holder (herdwatch) keeps the pane pending on background work (CI, review,
    # a marker) — reported through the explicit `waiting_on` metadata token.
    # herdr's own screen detection never emits this value.
    WAITING = "waiting"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AgentKey:
    server_id: str
    pane_id: str


@dataclass(frozen=True)
class WorkContext:
    """Small, display-only work identity supplied through Herdr metadata tokens."""

    source: str = ""
    item: str = ""
    run: str = ""
    url: str = ""

    @classmethod
    def from_tokens(cls, tokens: object) -> WorkContext:
        if not isinstance(tokens, dict):
            return cls()

        def bounded(name: str, limit: int) -> str:
            value = tokens.get(name)
            return value[:limit] if isinstance(value, str) else ""

        url = bounded("work_url", 2048)
        try:
            parsed = urlsplit(url)
        except ValueError:
            url = ""
        else:
            if parsed.scheme != "https" or not parsed.netloc:
                url = ""
        return cls(
            source=bounded("work_source", 64),
            item=bounded("work_item", 160),
            run=bounded("work_run", 160),
            url=url,
        )


# The ``subagents`` pane metadata token written by ``herdeck-subagent-hook``:
# "<running>/<total>". Small bounded integers only (the hook's spool keeps at
# most 20 entries); anything else is junk from some other writer.
_SUBAGENTS_TOKEN_RE = re.compile(r"\s*(\d{1,4})\s*/\s*(\d{1,4})\s*")


def parse_subagents_token(value: object) -> tuple[int, int]:
    """``(running, total)`` from a ``subagents`` metadata token; ``(0, 0)`` for
    anything malformed. ``running`` never exceeds ``total``."""
    if not isinstance(value, str):
        return 0, 0
    match = _SUBAGENTS_TOKEN_RE.fullmatch(value)
    if match is None:
        return 0, 0
    running, total = int(match.group(1)), int(match.group(2))
    if running > total:
        return 0, 0
    return running, total


# One subagent from the per-pane spool (``subagents`` wire field, bridge
# capability "subagents"). Statuses and text bounds mirror
# ``subagent_hook``; the runtime re-validates whatever the bridge sent.
SUBAGENT_STATUSES = ("running", "done", "failed", "stale")
SUBAGENTS_MAX = 20
_SUBAGENT_TEXT_MAX = {"id": 128, "provider": 16, "type": 64, "description": 160, "model": 64}
# C0/C1 controls (newlines included: every field is a single line) and the
# invisible characters that can reorder or hide text (bidi, zero-width, BOM).
_SUBAGENT_CONTROL_RE = re.compile(
    "[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]"
)


@dataclass(frozen=True)
class Subagent:
    id: str
    provider: str = ""
    type: str = ""
    description: str = ""
    model: str = ""
    depth: int | None = None
    status: str = "running"
    started_ms: int = 0
    ended_ms: int | None = None

    def to_wire(self) -> dict:
        return {
            "id": self.id,
            "provider": self.provider,
            "type": self.type,
            "description": self.description,
            "model": self.model,
            "depth": self.depth,
            "status": self.status,
            "started_ms": self.started_ms,
            "ended_ms": self.ended_ms,
        }


def _subagent_text(value: object, key: str) -> str:
    if not isinstance(value, str):
        return ""
    return _SUBAGENT_CONTROL_RE.sub("", value).strip()[: _SUBAGENT_TEXT_MAX[key]]


def _subagent_int(value: object) -> int | None:
    return value if type(value) is int and 0 <= value < 2**53 else None


def parse_subagents(value: object) -> tuple[Subagent, ...]:
    """Validated subagent list: malformed entries dropped, text sanitized and
    clipped, most recently started first, at most ``SUBAGENTS_MAX``."""
    if not isinstance(value, list):
        return ()
    out: list[Subagent] = []
    for raw in value[: SUBAGENTS_MAX * 4]:
        if not isinstance(raw, dict):
            continue
        entry_id = _subagent_text(raw.get("id"), "id")
        status = raw.get("status")
        started = _subagent_int(raw.get("started_ms"))
        if not entry_id or status not in SUBAGENT_STATUSES or started is None:
            continue
        depth = _subagent_int(raw.get("depth"))
        ended = _subagent_int(raw.get("ended_ms"))
        out.append(
            Subagent(
                id=entry_id,
                provider=_subagent_text(raw.get("provider"), "provider"),
                type=_subagent_text(raw.get("type"), "type"),
                description=_subagent_text(raw.get("description"), "description"),
                model=_subagent_text(raw.get("model"), "model"),
                depth=depth if depth is not None and depth <= 64 else None,
                status=status,
                started_ms=started,
                ended_ms=ended if ended is not None and ended >= started else None,
            )
        )
    out.sort(key=lambda s: (s.started_ms, s.id), reverse=True)
    return tuple(out[:SUBAGENTS_MAX])


@dataclass
class AgentState:
    key: AgentKey
    agent_type: str
    label: str
    status: Status
    project: str = ""
    repo: str = ""  # git repo name (from herdr worktree label)
    branch: str = ""  # git branch (from herdr worktree)
    workspace: str = ""  # herdr workspace label (workspace.list)
    tab: str = ""  # herdr tab label (tab.list)
    # Native Herdr positions, available from protocol 20 snapshots. They are
    # presentation hints only; None keeps older bridges fully compatible.
    workspace_order: int | None = None
    tab_order: int | None = None
    # Explicit Herdr 0.7.4 metadata tokens. ``waiting_on`` marks passive
    # background work; ``progress`` describes an actively working agent.
    waiting_on: str = ""
    progress: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    # herdr 0.8.2's native per-status labels ({status value: label}), the same
    # map herdr's own sidebar renders. Empty against an older herdr, and empty
    # on 0.8.2 until an integration sets one.
    state_labels: dict[str, str] = field(default_factory=dict)
    # Stable Herdr terminal identity. ``pane_id`` is a public location and may
    # be moved or recycled; long-lived controls must bind to this value too.
    terminal_id: str = ""
    title: str = ""
    display_agent: str = ""
    work: WorkContext = field(default_factory=WorkContext)
    backend: str = "herdr"
    capabilities: tuple[str, ...] = ()
    backend_revision: str = ""
    backend_actions: list[dict] = field(default_factory=list)
    preview: str = ""
    lifecycle: str = "active"
    activity: str = ""
    attention: str = ""
    completed_at: str = ""
    # Content hash of the project's favicon (bridge discovery), "" when none or
    # when the bridge predates the "project_icon" capability.
    project_icon: str = ""
    # Unix ms when the pane entered its current status, stamped by the bridge
    # ("status_since" capability). None from an older bridge or another
    # backend; the orchestrator then times the status from first sight.
    status_since_ms: int | None = None
    # herdr reports this pane as the focused one in its session. False from a
    # bridge that predates the field.
    focused: bool = False
    # Subagents the pane's agent has running / seen this session, from the
    # ``subagents`` metadata token (herdeck-subagent-hook). 0/0 without it.
    subagents_running: int = 0
    subagents_total: int = 0
    # The subagents themselves, most recent first (bridge capability
    # "subagents", read from the hook's spool on the agents' host). Empty
    # from an older bridge or when the pane has none.
    subagents: tuple[Subagent, ...] = ()
    # The bridge's blocked/done episode id (capability "events"), "" outside
    # one or from an older bridge. Stable across bridge restarts; answers and
    # lifecycle events refer to it.
    episode_id: str = ""
