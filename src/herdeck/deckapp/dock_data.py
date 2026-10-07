"""The desktop mini dock's data: GET /agent/list and GET /project-icon/<hash>.

The dock (desktop/src/lib/MiniDock.svelte) shows every active agent as a
favicon in a status ring and opens a card on hover. It needs the fleet as
DATA, not the 13 rendered tiles, so these helpers turn the source's agents
into JSON rows and a stored project favicon into a small PNG. Ordering is
the client's job; every row carries ``since_s`` so it can sort and tick.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping

from ..model import AgentKey, AgentState
from ..project_icons import ProjectIconStore, default_store

log = logging.getLogger(__name__)

# Content hash of a project favicon (project_icon_discovery.icon_hash). Use
# with fullmatch: "$" would accept a trailing newline.
ICON_HASH_RE = re.compile(r"[0-9a-f]{16}")
# Side of the PNG /project-icon serves: 40 px dock items at 2x, plus headroom.
PROJECT_ICON_PX = 96
# Icons are content-addressed (a hash never names other bytes), so the
# WebView may keep them for a day.
PROJECT_ICON_CACHE_CONTROL = "max-age=86400"


def row_title(agent: AgentState) -> str:
    """What the dock calls an agent: the herdr task title, else the
    workspace, the repo, the pane label."""
    return agent.title or agent.workspace or agent.repo or agent.label


def monogram(name: str) -> str:
    """The letter the deck's monogram shows (icons._monogram_image)."""
    return next((c for c in name if c.isalnum()), "?").upper()


def _icon_name(agent: AgentState) -> str:
    # Same seed as project_icons.tile_icon_fields: the [view.project_icons] key.
    return agent.repo or agent.project or agent.label


def agent_rows(
    agents: Iterable[AgentState],
    *,
    since: Mapping[AgentKey, float | None],
    connections: Mapping[str, bool],
    overrides: Mapping[str, str],
    store: ProjectIconStore | None = None,
) -> list[dict]:
    """One /agent/list row per ACTIVE agent, in the order given. The icon is
    resolved exactly like a tile's (override file first, then the bridge's
    hash once its bytes are stored), whatever [view].tile_icon says."""
    target = store if store is not None else default_store()
    rows: list[dict] = []
    for agent in agents:
        if agent.lifecycle != "active":
            continue
        name = _icon_name(agent)
        elapsed = since.get(agent.key)
        rows.append(
            {
                "server_id": agent.key.server_id,
                "pane_id": agent.key.pane_id,
                "agent_type": agent.agent_type,
                "title": row_title(agent),
                "repo": agent.repo,
                "workspace": agent.workspace,
                "branch": agent.branch,
                "tab": agent.tab,
                "status": agent.status.value,
                "since_s": int(elapsed) if elapsed is not None else 0,
                "waiting_on": agent.waiting_on,
                "subagents": agent.subagents_running,
                "offline": not connections.get(agent.key.server_id, True),
                "icon": target.resolve(name, agent.project_icon, overrides),
                "monogram": monogram(name),
            }
        )
    return rows


def list_signature(
    agents: Iterable[AgentState],
    connections: Mapping[str, bool],
    language: str,
    store: ProjectIconStore | None = None,
) -> tuple:
    """Everything a /agent/list row shows except the ticking ``since_s``: two
    calls return equal tuples exactly when the dock has nothing new to show.
    Order-independent; includes whether each wire icon's bytes are stored, so
    a favicon arriving after the snapshot counts as a change."""
    target = store if store is not None else default_store()
    rows = sorted(
        (
            a.key.server_id,
            a.key.pane_id,
            a.status.value,
            a.lifecycle,
            a.agent_type,
            a.title,
            a.workspace,
            a.repo,
            a.project,
            a.label,
            a.branch,
            a.tab,
            a.waiting_on,
            a.subagents_running,
            a.project_icon,
            bool(a.project_icon) and a.project_icon in target,
            a.status_since_ms,
        )
        for a in agents
    )
    return (tuple(rows), tuple(sorted(connections.items())), language)
