from __future__ import annotations

import json
from dataclasses import dataclass

from .model import AgentKey, AgentState, Status, WorkContext


def encode(msg: dict) -> str:
    return json.dumps(msg) + "\n"


def _status(value: str) -> Status:
    try:
        return Status(value)
    except ValueError:
        return Status.UNKNOWN


def _pane_to_state(server_id: str, pane: dict) -> AgentState:
    status = _status(pane.get("status", "unknown"))
    custom = pane.get("custom_status") or ""
    # A `working` pane carrying a custom_status is not the agent typing — it is
    # an external holder (herdwatch) keeping the pane pending on background
    # work (CI, review, a marker). Surface that as the distinct WAITING state.
    if status is Status.WORKING and custom:
        status = Status.WAITING
    wire_work = pane.get("work")
    work_labels = (
        {f"work.{key}": value for key, value in wire_work.items()}
        if isinstance(wire_work, dict)
        else {}
    )
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
        custom_status=custom,
        terminal_id=pane.get("terminal_id") or "",
        title=pane.get("title") or "",
        display_agent=pane.get("display_agent") or "",
        work=WorkContext.from_state_labels(work_labels),
    )


@dataclass
class Snapshot:
    server_id: str
    states: list[AgentState]
    protocol: int = 1
    capabilities: tuple[str, ...] = ()


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


def decode_inbound(
    raw: str,
) -> Snapshot | Event | Result | Error | TermFrame | TermClosed:
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
        return Snapshot(
            sid,
            [_pane_to_state(sid, p) for p in msg["panes"]],
            protocol,
            capabilities,
        )
    if kind == "event":
        sid = msg["server_id"]
        return Event(sid, _pane_to_state(sid, msg["pane"]))
    if kind == "result":
        return Result(msg["req"], msg.get("data", {}))
    if kind == "error":
        return Error(msg.get("message", ""))
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
    raise ValueError(f"unknown inbound message type: {kind}")
