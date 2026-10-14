"""Pure mapping of T3 orchestration V2 shell/detail data to Herdeck states.

No I/O here. Lifecycle rules port client-runtime ``threadSettled.ts``; the
status table is the one in the T3 V2 design spec. Timestamps are UTC and snooze
expiry is evaluated on every poll, even when the server emitted no event.
"""
import hashlib
import json
from datetime import datetime

from herdeck.model import AgentKey, AgentState, Status

PREVIEW_LIMIT = 12000
FORM_NOTICE = "Answer in T3 — this form needs the full conversation UI.\n"
_TASK_LABELS = {"subagent": "SUBAGENT", "command": "COMMAND",
                "monitor": "MONITOR", "background_task": "BACKGROUND"}


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def queued_start(shell, now):
    if shell.get("status") in ("preparing", "queued", "starting"):
        return True
    sent = timestamp(shell.get("latestUserMessageAt"))
    if sent is None or abs(now - sent) > 120 or shell.get("status") == "failed":
        return False
    return all(timestamp(shell.get(k)) is None or timestamp(shell[k]) < sent
               for k in ("latestRunRequestedAt", "latestRunStartedAt", "latestRunCompletedAt"))


def raised_hand(shell):
    if shell.get("pendingRuntimeRequest"):
        return True
    snoozed = timestamp(shell.get("snoozedAt"))
    if shell.get("status") == "failed":
        updated = timestamp(shell.get("updatedAt"))
        if snoozed is None or (updated is not None and updated > snoozed):
            return True
    done = timestamp(shell.get("latestRunCompletedAt"))
    return (shell.get("status") == "completed" and snoozed is not None
            and done is not None and done > snoozed)


def lifecycle(shell, now, archived=False):
    if shell.get("deletedAt"):
        return "deleted"
    if archived or shell.get("archivedAt"):
        return "archived"
    if shell.get("pendingRuntimeRequest"):
        return "active"
    if shell.get("settledOverride") == "settled":
        return "settled"
    until = timestamp(shell.get("snoozedUntil"))
    if until is None or until <= now or queued_start(shell, now) or raised_hand(shell):
        return "active"
    return "snoozed"


def classify(shell, now, life):
    """Return (status, attention, label, activity); first match wins."""
    req = shell.get("pendingRuntimeRequest")
    st = shell.get("status") or "idle"
    if life != "active":
        return Status.IDLE, "", life.upper(), "idle"
    if req:
        kind = req.get("kind")
        return Status.BLOCKED, ("input" if kind in ("user_input", "auth_refresh") else "approval"), "", st
    if st in ("preparing", "starting"):
        return Status.WORKING, "", "CONNECTING", st
    if st == "queued" or queued_start(shell, now):
        return Status.WORKING, "", "QUEUED", "queued"
    if st == "running":
        return Status.WORKING, "", "", st
    tasks = shell.get("pendingBackgroundTasks") or []
    if st == "waiting" or (st == "completed" and tasks):
        kind = (tasks[0].get("kind") if tasks else "") or ""
        return Status.WAITING, "", _TASK_LABELS.get(kind, "WAITING"), "waiting"
    if st == "failed":
        label = "LIMIT" if shell.get("lastErrorClass") == "usage_limit" else "ERROR"
        return Status.UNKNOWN, "error", label, st
    # T3's own "Plan Ready" (Sidebar.logic.ts): plan mode, an actionable plan
    # and a SETTLED latest run (the active/waiting rows above already returned).
    if (shell.get("interactionMode") == "plan" and shell.get("hasActionableProposedPlan")
            and not shell.get("activeRunId")):
        return Status.BLOCKED, "plan", "PLAN READY", st
    done_at, seen_at = timestamp(shell.get("latestRunCompletedAt")), timestamp(shell.get("lastVisitedAt"))
    if st == "completed" and done_at is not None and seen_at is not None and done_at > seen_at:
        return Status.DONE, "completion", "", st
    return Status.IDLE, "", "", st


def _items(detail):
    detail = detail or {}
    yield from detail.get("turnItems") or []
    for visible in detail.get("visibleTurnItems") or []:
        item = (visible or {}).get("item")
        if item:
            yield item


def request_item(detail, request_id):
    for item in _items(detail):
        if item.get("type") in ("approval_request", "user_input_request") and item.get("requestId") == request_id:
            return item
    return None


def live_request(detail, request_id):
    for req in (detail or {}).get("runtimeRequests") or []:
        if (req.get("id") == request_id and req.get("status") == "pending"
                and (req.get("responseCapability") or {}).get("type") == "live"):
            return True
    return False


def active_plan(detail):
    """Last active proposed plan as ``{"id", "status", "markdown"}`` or None."""
    plans = [p for p in (detail or {}).get("plans") or []
             if p.get("kind") == "proposed_plan" and p.get("status") == "active"]
    if not plans:
        return None
    plan = plans[-1]
    markdown = plan.get("markdown") or ""
    if not markdown:
        for item in _items(detail):
            if item.get("type") != "proposed_plan":
                continue
            by_id = plan.get("id") is not None and item.get("planId") == plan.get("id")
            by_node = plan.get("nodeId") is not None and item.get("nodeId") == plan.get("nodeId")
            if by_id or by_node:
                markdown = item.get("markdown") or ""
                break
    return {**plan, "markdown": markdown}


def last_assistant_text(detail):
    detail = detail or {}
    for item in reversed(list(detail.get("turnItems") or [])):
        if item.get("type") == "assistant_message" and item.get("text"):
            return str(item["text"])
    for message in reversed(detail.get("messages") or []):
        if message.get("role") == "assistant" and message.get("text"):
            return str(message["text"])
    return ""


def _input_preview(item, live):
    questions = item.get("questions") or []
    lines = []
    for q in questions:
        lines.append(f"{q.get('header') or ''}: {q.get('question') or ''}".strip(": "))
        lines.extend(f"- {o.get('label')}" for o in q.get("options") or [])
    single = len(questions) == 1 and not questions[0].get("multiSelect")
    return ("" if single and live else FORM_NOTICE) + "\n".join(lines)


def _preview(shell, detail, attention, life):
    req = shell.get("pendingRuntimeRequest")
    text = ""
    if req and life == "active":
        item = request_item(detail, req.get("id"))
        if item and item.get("type") == "user_input_request":
            text = _input_preview(item, live_request(detail, req.get("id")))
        elif item:
            text = str(item.get("prompt") or item.get("requestKind") or "")
        else:
            text = "Answer in T3 — details unavailable." if attention == "input" else "Approval requested"
    elif attention == "plan":
        plan = active_plan(detail)
        text = "Plan ready — choose how to implement it.\n" + (plan["markdown"] if plan else "")
    elif attention == "error":
        text = "T3 error: " + (shell.get("lastError") or "The latest run failed. Inspect T3 for details.")
    else:
        text = last_assistant_text(detail)
    if life != "active":
        text = f"{life.capitalize()} thread.\n" + text
    return text[:PREVIEW_LIMIT]


def thread_state(server_id, shell, detail, projects, epoch, *, features, now, archived=False):
    life = lifecycle(shell, now, archived)
    status, attention, label, activity = classify(shell, now, life)
    req = shell.get("pendingRuntimeRequest")
    item = request_item(detail, req.get("id")) if req else None
    plan = active_plan(detail)
    identity = [epoch, shell.get("id"), shell.get("status"), shell.get("activeRunId"),
                shell.get("latestRunId"), shell.get("latestRunCompletedAt"), req, item,
                [plan.get("id"), plan.get("status")] if plan else None,
                shell.get("settledOverride"), shell.get("settledAt"), shell.get("snoozedUntil"),
                shell.get("snoozedAt"), shell.get("archivedAt"), shell.get("deletedAt"),
                shell.get("lastVisitedAt"), shell.get("interactionMode"), features]
    revision = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()
    project = (projects or {}).get(shell.get("projectId")) or {}
    title = shell.get("title") or "T3 conversation"
    return AgentState(
        key=AgentKey(server_id, shell["id"]),
        agent_type=(shell.get("modelSelection") or {}).get("instanceId")
        or shell.get("providerInstanceId") or "default",
        label=title, title=title, status=status,
        project=project.get("title") or "", repo=project.get("workspaceRoot") or "",
        branch=shell.get("branch") or "", display_agent="T3 Code", backend="t3",
        capabilities=(), backend_revision=revision, backend_actions=[],
        preview=_preview(shell, detail, attention, life), lifecycle=life,
        activity=activity, attention=attention,
        completed_at=shell.get("latestRunCompletedAt") or "",
        state_labels={status.value: label} if label else {},
    )
