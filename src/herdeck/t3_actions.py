"""Versioned semantic controls. Never translate T3 decisions to terminal keys."""
import re
import uuid
from datetime import UTC, datetime

from .model import Status


def negotiated_features(descriptor):
    version = descriptor.get("serverVersion", "")
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    # A new minor/major contract requires a deliberate compatibility review.
    supported = bool(match and tuple(map(int, match.groups()))[:2] == (0, 0)
                     and int(match[3]) >= 31)
    capabilities = descriptor.get("capabilities") or {}
    return {"core": supported, "extended": supported and int(match[3]) >= 38,
            "settle": supported and capabilities.get("threadSettlement") is True,
            "snooze": supported and capabilities.get("threadSnooze") is True}


def extend_actions(actions, thread, life, status, attention, activity, features, now, completed):
    def add(action, label, payload=None, confirm=False):
        actions.append(dict(id=action, label=label, payload=payload or {}, confirm=confirm))
    if life in ("archived", "deleted"):
        return
    if life == "active" and attention == "completion":
        add("acknowledge", "Mark seen", {"completedAt": completed})
    if features.get("extended") and life == "active":
        if attention == "plan":
            plans = [p for p in thread.get("proposedPlans", []) if not p.get("implementedAt") and p.get("planMarkdown")]
            if plans and all(thread.get(k) for k in ("runtimeMode", "modelSelection")):
                add("implement_plan", "Implement plan", {"planId": plans[-1]["id"]}, True)
        if activity in ("working", "monitoring") and not (thread.get("session") or {}).get("activeTurnId"):
            add("session_stop", "Stop session", confirm=True)
    if features.get("settle"):
        if life == "settled":
            add("unsettle", "Reopen")
        elif life == "active" and status in (Status.IDLE, Status.DONE, Status.UNKNOWN):
            add("settle", "Settle")
    if features.get("snooze"):
        if life == "snoozed":
            add("unsnooze", "Wake now")
        elif life == "active" and attention not in ("approval", "input") and activity != "queued":
            add("snooze", "Snooze 1h")


def semantic_command(action, payload, thread):
    if action in ("settle", "unsettle", "snooze", "unsnooze"):
        command = {"type": "thread." + action}
        if action in ("unsettle", "unsnooze"):
            command["reason"] = "user"
        elif action == "snooze":
            command["snoozedUntil"] = datetime.fromtimestamp(datetime.now(UTC).timestamp() + 3600, UTC).isoformat()
        return command
    if action == "session_stop":
        return {"type": "thread.session.stop"}
    if action == "implement_plan":
        plan = next(p for p in thread["proposedPlans"] if p["id"] == payload["planId"])
        return {"type": "thread.turn.start", "runtimeMode": thread["runtimeMode"],
                "interactionMode": "default", "modelSelection": thread["modelSelection"],
                "sourceProposedPlan": {"threadId": thread["id"], "planId": plan["id"]},
                "message": {"messageId": uuid.uuid4().hex, "role": "user",
                            "text": "Implement the following plan:\n\n" + plan["planMarkdown"], "attachments": []}}
    return {"type": "thread.user-input.respond" if action == "answer" else "thread.approval.respond", **payload}
