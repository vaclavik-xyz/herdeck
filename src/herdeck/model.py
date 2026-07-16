from __future__ import annotations

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
    # Explicit Herdr 0.7.4 metadata tokens. ``waiting_on`` marks passive
    # background work; ``progress`` describes an actively working agent.
    waiting_on: str = ""
    progress: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    # Stable Herdr terminal identity. ``pane_id`` is a public location and may
    # be moved or recycled; long-lived controls must bind to this value too.
    terminal_id: str = ""
    title: str = ""
    display_agent: str = ""
    work: WorkContext = field(default_factory=WorkContext)
