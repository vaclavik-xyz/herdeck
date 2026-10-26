"""The desktop mini dock's data: GET /agent/list and GET /project-icon/<hash>.

The dock (desktop/src/lib/MiniDock.svelte) shows every active agent as a
favicon in a status ring and opens a card on hover. It needs the fleet as
DATA, not the 13 rendered tiles, so these helpers turn the source's agents
into JSON rows and a stored project favicon into a small PNG. Ordering is
the client's job; every row carries ``since_s`` so it can sort and tick.
"""

from __future__ import annotations

import io
import logging
import re
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath

from ..model import AgentKey, AgentState, Status
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
# An agent type as it may appear in /agent-mark/<type> (lowercase slug).
AGENT_TYPE_RE = re.compile(r"[a-z0-9_-]{1,32}")
# The /project-icon "hash" of the bundled no-project glyph (the tiles'
# project_icons.NO_PROJECT_ICON, icons.no_project_image): a fixed name in the
# hash format, so the dock client and the image proxy take it like any
# favicon. Bump it when the glyph's pixels change (the WebView caches a day).
NO_PROJECT_ROW_ICON = "0000000000000001"


def icon_fallback_of(view) -> str:
    """[view].project_icon_fallback as "monogram" | "agent". Read defensively:
    the key is introduced by the deck-themes work and is not parsed by
    settings yet, so until then this is always "monogram"; anything unknown
    is the default."""
    value = getattr(view, "project_icon_fallback", "monogram")
    return "agent" if value == "agent" else "monogram"


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


def row_repo(agent: AgentState) -> str:
    """The repo the dock shows and groups by. A T3 thread's repo is its
    project's workspaceRoot path: show its T3 project name like the deck
    tile does (layout.compose_line), else the path's last part. A
    project-less thread's project is the "No project" label."""
    if agent.backend == "t3":
        return agent.project or PurePosixPath(agent.repo).name
    return agent.repo


def waiting_on(agent: AgentState) -> str:
    """What holds a waiting agent: herdeck's `waiting_on` holder label, else
    the backend's own waiting label (T3: CI, SUBAGENT, …)."""
    if agent.waiting_on or agent.status is not Status.WAITING:
        return agent.waiting_on
    return agent.state_labels.get(Status.WAITING.value, "")


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
                "backend": agent.backend,
                "title": row_title(agent),
                "repo": row_repo(agent),
                "workspace": agent.workspace,
                "branch": agent.branch,
                "tab": agent.tab,
                "status": agent.status.value,
                "since_s": int(elapsed) if elapsed is not None else 0,
                "waiting_on": waiting_on(agent),
                "subagents": agent.subagents_running,
                "offline": not connections.get(agent.key.server_id, True),
                "icon": target.resolve(name, agent.project_icon, overrides)
                or (NO_PROJECT_ROW_ICON if agent.no_project else None),
                "monogram": monogram(row_repo(agent) or name),
            }
        )
    return rows


def list_signature(
    agents: Iterable[AgentState],
    connections: Mapping[str, bool],
    language: str,
    store: ProjectIconStore | None = None,
    *,
    icon_fallback: str = "monogram",
    overrides: Mapping[str, str] | None = None,
) -> tuple:
    """Everything a /agent/list row shows except the ticking ``since_s``: two
    calls return equal tuples exactly when the dock has nothing new to show.
    Order-independent; includes whether each wire icon's bytes are stored, so
    a favicon arriving after the snapshot counts as a change. The icon
    fallback and the [view.project_icons] overrides are part of it too, so a
    config edit wakes the dock."""
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
            waiting_on(a),
            a.backend,
            a.no_project,
            a.subagents_running,
            a.project_icon,
            bool(a.project_icon) and a.project_icon in target,
            a.status_since_ms,
        )
        for a in agents
    )
    return (
        tuple(rows),
        tuple(sorted(connections.items())),
        language,
        icon_fallback,
        tuple(sorted((overrides or {}).items())),
    )


def project_icon_png(icon_hash: str, store: ProjectIconStore | None = None) -> tuple[int, bytes | None]:
    """GET /project-icon/<hash>: (HTTP status, PNG bytes). The stored favicon
    is decoded and normalised by the deck's own decoder (SVG references to
    outside resources refused there) and re-encoded as a PROJECT_ICON_PX PNG:
    the WebView never sees raw SVG/ICO bytes from someone else's repo.
    400 malformed hash, 404 unknown or undecodable (the dock draws its
    monogram)."""
    if not ICON_HASH_RE.fullmatch(icon_hash):
        return 400, None
    if icon_hash == NO_PROJECT_ROW_ICON:
        return 200, _no_project_png()
    stored = (store if store is not None else default_store()).get(icon_hash)
    if stored is None:
        return 404, None
    # Lazy: Pillow is not needed to import herdeck.deckapp (see server.py).
    from PIL import Image

    from ..icons import decode_project_icon

    try:
        img = decode_project_icon(stored)
    except Exception as exc:
        log.debug("project icon %s could not be decoded: %s", icon_hash, exc)
        return 404, None
    img = img.resize((PROJECT_ICON_PX, PROJECT_ICON_PX), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return 200, buf.getvalue()


def _no_project_png() -> bytes:
    """The bundled no-project glyph as a PROJECT_ICON_PX PNG."""
    from PIL import Image

    from ..icons import no_project_image

    img = no_project_image().resize((PROJECT_ICON_PX, PROJECT_ICON_PX), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def agent_mark_png(agent_type: str, icons) -> tuple[int, bytes | None]:
    """GET /agent-mark/<type>: the deck's own agent logo as a PROJECT_ICON_PX
    PNG, the dock's stand-in for a project without a favicon when
    [view].project_icon_fallback = "agent". 400 for a malformed type, 404 when
    the mark cannot be rendered (the dock then draws its monogram). A
    well-formed but unknown type renders the provider's generic letter mark."""
    if not AGENT_TYPE_RE.fullmatch(agent_type or ""):
        return 400, None
    try:
        img = icons.agent_mark_image(agent_type, PROJECT_ICON_PX)
        buf = io.BytesIO()
        img.save(buf, "PNG")
    except Exception as exc:
        log.debug("agent mark %s could not be rendered: %s", agent_type, exc)
        return 404, None
    return 200, buf.getvalue()
