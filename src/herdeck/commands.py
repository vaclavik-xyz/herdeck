from __future__ import annotations

from dataclasses import dataclass, field

from .config import AnswerProfile, Config
from .model import AgentState


@dataclass
class Command:
    kind: str  # list|read|focus|act_if_blocked|act_force|send_text|start|switch_profile
    server_id: str
    pane_id: str | None = None
    source: str | None = None
    keys: list[str] = field(default_factory=list)
    text: str | None = None  # for send_text (macros) / start (agent name)


def command_to_msg(cmd: Command, req: str | None) -> dict:
    """Encode a Command into the bridge wire message. `req` is ignored for `list`."""
    if cmd.kind == "list":
        return {"type": "list"}
    if cmd.kind == "read":
        return {"type": "read", "req": req, "pane_id": cmd.pane_id, "source": cmd.source}
    if cmd.kind == "focus":
        return {"type": "focus", "req": req, "pane_id": cmd.pane_id}
    if cmd.kind == "send_text":
        return {"type": "send_text", "req": req, "pane_id": cmd.pane_id, "text": cmd.text}
    if cmd.kind == "start":
        return {"type": "start", "req": req, "name": cmd.text, "argv": cmd.keys}
    if cmd.kind in ("act_if_blocked", "act_force"):
        return {
            "type": "act",
            "req": req,
            "pane_id": cmd.pane_id,
            "keys": cmd.keys,
            "guard": cmd.kind == "act_if_blocked",
        }
    raise ValueError(f"unknown command kind: {cmd.kind}")


def profile_for(config: Config, agent_type: str) -> AnswerProfile:
    """Pick the answer profile for an agent type, falling back to 'default'."""
    return config.profiles.get(agent_type, config.profiles["default"])


def build_action_command(
    action: str, agent: AgentState, profile: AnswerProfile, *, force: bool, always: bool
) -> Command:
    """Map a high-level action (approve/deny/stop) + agent + profile -> Command."""
    if action == "approve":
        keys = profile.approve_always if always else profile.approve
        kind = "act_force" if force else "act_if_blocked"
    elif action == "deny":
        keys = profile.deny
        kind = "act_force" if force else "act_if_blocked"
    elif action == "stop":
        keys = profile.stop
        kind = "act_force"
    else:
        raise ValueError(f"unknown action: {action}")
    return Command(kind, agent.key.server_id, agent.key.pane_id, keys=list(keys))
