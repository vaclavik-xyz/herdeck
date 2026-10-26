"""Pure mapping of T3 orchestration V2 shell/detail data to Herdeck states.

No I/O here. Lifecycle rules port client-runtime ``threadSettled.ts``; the
status table is the one in the T3 V2 design spec. Timestamps are UTC and snooze
expiry is evaluated on every poll, even when the server emitted no event.
"""
import hashlib
import json
import re
from datetime import datetime

from herdeck.i18n import tr
from herdeck.model import AgentKey, AgentState, Status, parse_subagents

PREVIEW_LIMIT = 12000
_TASK_LABELS = {"subagent": "SUBAGENT", "command": "COMMAND",
                "monitor": "MONITOR", "background_task": "BACKGROUND"}
# OrchestrationV2Subagent.status -> Herdeck Subagent.status. pending, running
# and waiting are active work (upstream isOrchestrationV2WorkActive); idle is
# resumable but no longer working; cancelled and interrupted ended without a
# result. Any other status is dropped.
SUBAGENT_STATUS = {"pending": "running", "running": "running", "waiting": "running",
                   "idle": "done", "completed": "done",
                   "failed": "failed", "cancelled": "failed", "interrupted": "failed"}
_SUBAGENT_ID_MAX = 128  # model._SUBAGENT_TEXT_MAX["id"]
# A background command whose description reads like waiting on a PR's checks
# ("Wait for PR 649 checks to finish", "gh pr checks --watch"). Needs the
# waiting verb too: "Start dev server to check the PR UI" must not match.
_CI_CMD = re.compile(r"\bgh (pr checks|run watch)\b", re.IGNORECASE)
_CI_WAIT = re.compile(r"\b(wait|watch)", re.IGNORECASE)
_CI_SUBJECT = re.compile(r"\b(pr|pull request|checks?|ci)\b", re.IGNORECASE)


def _ci_wait_text(text):
    return bool(_CI_CMD.search(text) or (_CI_WAIT.search(text) and _CI_SUBJECT.search(text)))


def holds_completion(task):
    """Mirrors upstream backgroundWorkHoldsCompletion: a task the agent awaits
    (t3_background_wait, ``awaited``) holds a finished run, so T3 shows the
    thread as Waiting. Unawaited commands (a dev server left running) and
    monitors do not; subagents, background tasks and unknown kinds do."""
    return task.get("awaited") is True or task.get("kind") not in ("command", "monitor")


def wait_label(tasks):
    if any(t.get("kind") == "command" and _ci_wait_text(t.get("description") or "") for t in tasks):
        return "CI"
    kind = (tasks[0].get("kind") if tasks else "") or ""
    return _TASK_LABELS.get(kind, "WAITING")


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


def published(shell, now, archived=False):
    """Whether a thread gets a tile of its own.

    T3 hides subagent children (``lineage.relationshipToParent == "subagent"``)
    in its sidebar, notifications and mobile list; a provider-native subagent's
    approval is asked on the parent's thread and run. A child that holds a
    request itself (a delegate_task child runs its own session) stays a tile
    while the request is pending: T3 does not roll it up to the parent.
    """
    lineage = shell.get("lineage")
    if not isinstance(lineage, dict) or lineage.get("relationshipToParent") != "subagent":
        return True
    return bool(shell.get("pendingRuntimeRequest")) and lifecycle(shell, now, archived) == "active"


def _ms(value):
    seconds = timestamp(value)
    return None if seconds is None else round(seconds * 1000)


def thread_subagents(detail):
    """``(subagents, running, total)`` from the projection's ``subagents``.

    Each projection lists its own direct subagents (nested ones live in the
    hidden child's projection, which is not read). The bounded detail keeps the
    active ones and those of its recent runs. Child shells are no substitute:
    a provider-native child has no runs, so its shell status stays ``idle``.
    The counts are taken before the shared ``SUBAGENTS_MAX`` clip.
    """
    tasks = (detail or {}).get("subagents")
    raw = []
    for task in tasks if isinstance(tasks, list) else []:
        if not isinstance(task, dict) or not isinstance(task.get("id"), str) or not task["id"]:
            continue
        status = SUBAGENT_STATUS.get(task.get("status"))
        started = _ms(task.get("startedAt"))
        if started is None:
            started = _ms(task.get("updatedAt"))  # pending: not started yet
        if status is None or started is None:
            continue
        sid = task["id"]
        if len(sid) > _SUBAGENT_ID_MAX:  # clipping could merge two ids
            sid = "t3:" + hashlib.sha256(sid.encode()).hexdigest()
        prompt = task.get("prompt") if isinstance(task.get("prompt"), str) else ""
        selection = task.get("modelSelection") if isinstance(task.get("modelSelection"), dict) else {}
        raw.append({"id": sid, "provider": task.get("driver"), "type": task.get("title"),
                    "description": next((line.strip() for line in prompt.splitlines() if line.strip()), ""),
                    "model": task.get("model") or selection.get("model"), "depth": 1,
                    "status": status, "started_ms": started,
                    "ended_ms": None if status == "running" else _ms(task.get("completedAt"))})
    running = sum(1 for r in raw if r["status"] == "running")
    return parse_subagents(raw), running, len(raw)


def ci_wait(shell, tasks):
    """True when a finished run left a command polling an open linked PR.

    T3 does not let a command hold a completed run (it may be a dev server),
    so an agent that backgrounds ``gh pr checks --watch`` reads as done while
    CI is still running. Both signals are required: the description alone is
    free agent text, and an open PR alone says nothing about a dev server."""
    if shell.get("status") not in ("completed", "waiting"):
        return False
    if not any(t.get("kind") == "command" and _ci_wait_text(t.get("description") or "")
               for t in tasks):
        return False
    return any(((pr.get("snapshot") or {}).get("state") == "open")
               for pr in shell.get("pullRequests") or [] if isinstance(pr, dict))


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
    if ci_wait(shell, tasks):
        return Status.WAITING, "", "CI", "waiting"
    if st == "completed":
        tasks = [t for t in tasks if holds_completion(t)]
    if st == "waiting" or (st == "completed" and tasks):
        return Status.WAITING, "", wait_label(tasks), "waiting"
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


def _input_preview(item, live, lang):
    questions = item.get("questions") or []
    lines = []
    for q in questions:
        lines.append(f"{q.get('header') or ''}: {q.get('question') or ''}".strip(": "))
        lines.extend(f"- {o.get('label')}" for o in q.get("options") or [])
    single = len(questions) == 1 and not questions[0].get("multiSelect")
    return ("" if single and live else tr(lang, "t3.form_notice") + "\n") + "\n".join(lines)


def _preview(shell, detail, attention, life, lang):
    req = shell.get("pendingRuntimeRequest")
    text = ""
    if req and life == "active":
        item = request_item(detail, req.get("id"))
        if item and item.get("type") == "user_input_request":
            text = _input_preview(item, live_request(detail, req.get("id")), lang)
        elif item:
            text = str(item.get("prompt") or item.get("requestKind") or "")
        else:
            text = tr(lang, "t3.input_unavailable" if attention == "input" else "t3.approval_requested")
    elif attention == "plan":
        plan = active_plan(detail)
        text = tr(lang, "t3.plan_ready") + "\n" + (plan["markdown"] if plan else "")
    elif attention == "error":
        text = tr(lang, "t3.error", error=shell.get("lastError") or tr(lang, "t3.error_default"))
    else:
        text = last_assistant_text(detail)
    if life != "active":
        text = tr(lang, "t3.life_" + life) + "\n" + text
    return text[:PREVIEW_LIMIT]


def is_no_project(project) -> bool:
    """True for the bucket T3 files a thread created without a project under.

    T3 models it as an ordinary project titled "No project" (the title may be
    localized, so it is never read): no repository identity and a workspace
    root that is T3's scratch dir (``<home>/.t3/scratch``). A thread whose
    project is missing or unknown counts as project-less too.
    """
    if not isinstance(project, dict) or not project:
        return True
    if project.get("repositoryIdentity"):
        return False
    root = project.get("workspaceRoot")
    if not isinstance(root, str):
        return False
    parts = [p for p in root.replace("\\", "/").split("/") if p]
    return len(parts) >= 2 and parts[-2] == ".t3" and parts[-1] == "scratch"


def thread_state(server_id, shell, detail, projects, epoch, *, features, now, archived=False, lang="en"):
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
    no_project = is_no_project(project)
    title = shell.get("title") or tr(lang, "t3.untitled")
    # Not part of the revision: no action depends on the subagent list.
    subagents, running, total = thread_subagents(detail)
    return AgentState(
        key=AgentKey(server_id, shell["id"]),
        agent_type=(shell.get("modelSelection") or {}).get("instanceId")
        or shell.get("providerInstanceId") or "default",
        label=title, title=title, status=status,
        # A project-less thread gets Herdeck's own localized label, not T3's
        # bucket title; layout.compose_line then headlines the thread title.
        project=tr(lang, "t3.no_project") if no_project else project.get("title") or "",
        repo=project.get("workspaceRoot") or "", no_project=no_project,
        branch=shell.get("branch") or "", display_agent="T3 Code", backend="t3",
        capabilities=(), backend_revision=revision, backend_actions=[],
        preview=_preview(shell, detail, attention, life, lang), lifecycle=life,
        activity=activity, attention=attention,
        completed_at=shell.get("latestRunCompletedAt") or "",
        state_labels={status.value: label} if label else {},
        subagents_running=running, subagents_total=total, subagents=subagents,
    )
