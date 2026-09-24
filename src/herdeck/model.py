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
