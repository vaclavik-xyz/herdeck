from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass

from .model import AgentKey, AgentState, Status, WorkContext
from .project_icon_discovery import ICON_MIMES, MAX_ICON_BYTES, icon_hash

_ICON_HASH_RE = re.compile(r"[0-9a-f]{16}")
# Base64 length of MAX_ICON_BYTES (whole 4-char groups): a cheap pre-decode cap.
_MAX_ICON_B64 = 4 * ((MAX_ICON_BYTES + 2) // 3)


def encode(msg: dict) -> str:
    return json.dumps(msg) + "\n"


def _status(value: str) -> Status:
    try:
        return Status(value)
    except ValueError:
        return Status.UNKNOWN


def _order(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _icon_ref(value: object) -> str:
    """A pane's favicon hash, or "" for anything that is not one."""
    return value if isinstance(value, str) and _ICON_HASH_RE.fullmatch(value) else ""


def _pane_to_state(server_id: str, pane: dict) -> AgentState:
    status = _status(pane.get("status", "unknown"))
    waiting_on = pane.get("waiting_on") or ""
    # A user-facing block remains more important. Otherwise explicit passive
    # metadata owns the derived state across Herdr's idle/done/working states.
    if status in (Status.WORKING, Status.IDLE, Status.DONE) and waiting_on:
        status = Status.WAITING
    metadata = pane.get("metadata") if isinstance(pane.get("metadata"), dict) else {}
    state_labels = pane.get("state_labels") if isinstance(pane.get("state_labels"), dict) else {}
    wire_work = pane.get("work") if isinstance(pane.get("work"), dict) else {}
    raw_capabilities = pane.get("capabilities")
    capabilities = (
        tuple(value for value in raw_capabilities if isinstance(value, str))
        if isinstance(raw_capabilities, list)
        else ()
    )
    work_tokens = {
        "work_source": wire_work.get("source", ""),
        "work_item": wire_work.get("item", ""),
        "work_run": wire_work.get("run", ""),
        "work_url": wire_work.get("url", ""),
    }
    return AgentState(
        key=AgentKey(server_id, pane["pane_id"]),
        agent_type=pane.get("agent_type", "default"),
        label=pane.get("label", ""),
        status=status,
        project=pane.get("project", ""),
        repo=pane.get("repo", ""),
        branch=pane.get("branch", ""),
        workspace=pane.get("workspace", ""),
        tab=pane.get("tab", ""),
        workspace_order=_order(pane.get("workspace_order")),
        tab_order=_order(pane.get("tab_order")),
        waiting_on=waiting_on,
        progress=pane.get("progress") or "",
        metadata={str(key): str(value) for key, value in metadata.items()},
        # Cosmetic labels: a malformed entry is dropped, never coerced into
        # tile text (a stray non-string would read as nonsense on the deck).
        state_labels={
            key: value
            for key, value in state_labels.items()
            if isinstance(key, str) and isinstance(value, str)
        },
        terminal_id=pane.get("terminal_id") or "",
        title=pane.get("title") or "",
        display_agent=pane.get("display_agent") or "",
        work=WorkContext.from_tokens(work_tokens),
        capabilities=capabilities,
        project_icon=_icon_ref(pane.get("project_icon")),
        focused=pane.get("focused") is True,
    )


# The bridge<->runtime wire protocol this code speaks. Additive pane fields
# are advertised as capabilities instead; bumping this is a breaking change a
# runtime older than the bridge reports as "unsupported protocol".
WIRE_PROTOCOL = 3


@dataclass
class Snapshot:
    server_id: str
    states: list[AgentState]
    protocol: int = 1
    capabilities: tuple[str, ...] = ()
    # The bridge's herdeck package version (None from an older bridge).
    herdeck_version: str | None = None


@dataclass
class Event:
    server_id: str
    state: AgentState


@dataclass
class Result:
    req: str
    data: dict


@dataclass
class Error:
    message: str
    # The request the bridge refused, when it names one (e.g. a read-only
    # token rejecting a mutating message). None for connection-level errors.
    req: str | None = None


@dataclass
class TermFrame:
    """One live-terminal frame (base64 ANSI, passed through from herdr)."""

    req: str
    seq: int
    full: bool
    cols: int
    rows: int
    data: str


@dataclass
class TermClosed:
    req: str
    reason: str
    stop_remote: bool = False


@dataclass
class ProjectIcon:
    """A project favicon's bytes, sent once per content hash to clients that
    opted in with ``{"type": "list", "features": ["project_icon"]}``."""

    server_id: str
    hash: str
    mime: str
    data: bytes


@dataclass
class Progress:
    """An in-flight long request's progress line (bridge self-update)."""

    req: str
    stage: str
    message: str


@dataclass
class Unknown:
    """A frame type this client does not know (a newer bridge). Ignored."""

    type: str


def _decode_project_icon(msg: dict) -> ProjectIcon:
    sid, digest, mime, data = (msg.get(k) for k in ("server_id", "hash", "mime", "data"))
    if not isinstance(sid, str) or not isinstance(digest, str) or not _ICON_HASH_RE.fullmatch(digest):
        raise ValueError("malformed project_icon frame (server_id/hash)")
    if not isinstance(mime, str) or mime not in ICON_MIMES:
        raise ValueError(f"malformed project_icon frame (mime {mime!r})")
    if not isinstance(data, str) or len(data) > _MAX_ICON_B64:
        raise ValueError("malformed project_icon frame (data)")
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("malformed project_icon frame (base64)") from exc
    if not 1 <= len(raw) <= MAX_ICON_BYTES or icon_hash(raw) != digest:
        raise ValueError("malformed project_icon frame (size/hash)")
    return ProjectIcon(sid, digest, mime, raw)


def decode_inbound(
    raw: str,
) -> (
    Snapshot | Event | Result | Error | TermFrame | TermClosed | ProjectIcon | Progress | Unknown
):
    msg = json.loads(raw)
    kind = msg["type"]
    if kind == "snapshot":
        sid = msg["server_id"]
        protocol = msg.get("protocol", 1)
        if type(protocol) is not int or protocol < 1:
            protocol = 1
        raw_capabilities = msg.get("capabilities", [])
        capabilities = (
            tuple(value for value in raw_capabilities if isinstance(value, str))
            if isinstance(raw_capabilities, list)
            else ()
        )
        version = msg.get("herdeck_version")
        return Snapshot(
            sid,
            [_pane_to_state(sid, p) for p in msg["panes"]],
            protocol,
            capabilities,
            version[:64] if isinstance(version, str) and version else None,
        )
    if kind == "event":
        sid = msg["server_id"]
        return Event(sid, _pane_to_state(sid, msg["pane"]))
    if kind == "result":
        return Result(msg["req"], msg.get("data", {}))
    if kind == "error":
        req = msg.get("req")
        return Error(msg.get("message", ""), req=req if isinstance(req, str) and req else None)
    if kind == "term_frame":
        req = msg.get("req")
        if not isinstance(req, str) or not req:
            raise ValueError("terminal frame missing request id")
        valid = (
            type(msg.get("seq")) is int
            and msg["seq"] >= 0
            and type(msg.get("full")) is bool
            and type(msg.get("cols")) is int
            and msg["cols"] > 0
            and type(msg.get("rows")) is int
            and msg["rows"] > 0
            and isinstance(msg.get("data"), str)
        )
        if not valid:
            return TermClosed(req, "invalid terminal frame", stop_remote=True)
        return TermFrame(
            req,
            msg["seq"],
            msg["full"],
            msg["cols"],
            msg["rows"],
            msg["data"],
        )
    if kind == "term_closed":
        req = msg.get("req")
        if not isinstance(req, str) or not req:
            raise ValueError("terminal close missing request id")
        reason = msg.get("reason", "")
        return TermClosed(req, reason if isinstance(reason, str) else "preview closed")
    if kind == "project_icon":
        return _decode_project_icon(msg)
    if kind == "progress":
        req, stage, message = (msg.get(k) for k in ("req", "stage", "message"))
        if not isinstance(req, str) or not req:
            raise ValueError("progress frame missing request id")
        return Progress(
            req,
            stage[:32] if isinstance(stage, str) else "",
            message[:300] if isinstance(message, str) else "",
        )
    # Forward compatibility: a newer bridge may add frame types. Raising here
    # would reach Connector's on_error (ctl fails every pending request on it).
    return Unknown(str(kind))
