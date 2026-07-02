from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


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
