from __future__ import annotations

import json
from dataclasses import dataclass

from .model import AgentKey, AgentState, Status


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
    )


@dataclass
class Snapshot:
    server_id: str
    states: list[AgentState]


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


def decode_inbound(raw: str) -> Snapshot | Event | Result | Error:
    msg = json.loads(raw)
    kind = msg["type"]
    if kind == "snapshot":
        sid = msg["server_id"]
        return Snapshot(sid, [_pane_to_state(sid, p) for p in msg["panes"]])
    if kind == "event":
        sid = msg["server_id"]
        return Event(sid, _pane_to_state(sid, msg["pane"]))
    if kind == "result":
        return Result(msg["req"], msg.get("data", {}))
    if kind == "error":
        return Error(msg.get("message", ""))
    raise ValueError(f"unknown inbound message type: {kind}")
