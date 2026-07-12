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
    # a marker) — reported to herdr as `working` + a custom_status label.
    # herdr's own screen detection never emits this value.
    WAITING = "waiting"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AgentKey:
    server_id: str
    pane_id: str


@dataclass(frozen=True)
class WorkContext:
    """Small, display-only work identity supplied through Herdr state labels."""

    source: str = ""
    item: str = ""
    run: str = ""
    url: str = ""

    @classmethod
    def from_state_labels(cls, labels: object) -> WorkContext:
        if not isinstance(labels, dict):
            return cls()

        def bounded(name: str, limit: int) -> str:
            value = labels.get(name)
            return value[:limit] if isinstance(value, str) else ""

        url = bounded("work.url", 2048)
        try:
            parsed = urlsplit(url)
        except ValueError:
            url = ""
        else:
            if parsed.scheme != "https" or not parsed.netloc:
                url = ""
        return cls(
            source=bounded("work.source", 64),
            item=bounded("work.item", 160),
            run=bounded("work.run", 160),
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
    # Label asserted via `herdr pane report-agent --custom-status` (herdwatch:
    # "⏳ ci", "⏳ review +1"); empty when no source holds the pane.
    custom_status: str = ""
    # Stable Herdr terminal identity. ``pane_id`` is a public location and may
    # be moved or recycled; long-lived controls must bind to this value too.
    terminal_id: str = ""
    title: str = ""
    display_agent: str = ""
    work: WorkContext = field(default_factory=WorkContext)
