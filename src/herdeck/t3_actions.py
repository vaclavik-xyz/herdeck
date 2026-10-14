"""T3 orchestration V2: protocol negotiation, offered deck actions, command payloads.

Pure functions. Commands are built without ``commandId``; the connector adds
(and remembers) it so a retry of an unknown-outcome dispatch is idempotent.
``thread.stop`` is server-internal and never built; Stop is ``run.interrupt``.
"""
import uuid
from datetime import UTC, datetime

from .model import Status
from .t3_state import active_plan, live_request, request_item, timestamp

DEFAULT_OPTIONS = [{"decision": "accept", "label": "Approve"},
                   {"decision": "decline", "label": "Deny"}]
DECISIONS = ("accept", "acceptForSession", "acceptAlways", "decline", "cancel")
APPROVE_IDS = {"accept": "approve", "acceptForSession": "approve_session",
               "acceptAlways": "approve_always"}
MESSAGE_ERROR = "T3 message must contain 1–4096 characters"
PLAN_ERROR = "T3 plan is too long to implement from the deck"
# Upstream PROVIDER_SEND_TURN_MAX_INPUT_CHARS (chatAttachment.ts); bounds the plan message.
PLAN_MAX_CHARS = 120_000


class Incompatible(Exception):
    pass


def negotiate(descriptor):
    version = descriptor.get("orchestrationProtocolVersion")
    if version != 2:
        raise Incompatible(f"T3 server speaks orchestration protocol {1 if version is None else version}; "
                           "Herdeck needs 2 (T3 0.0.46+)")
    caps = descriptor.get("capabilities") or {}
    return {"settle": caps.get("threadSettlement") is True,
            "snooze": caps.get("threadSnooze") is True,
            "visit": caps.get("threadVisitedTracking") is True,
            "resolved_context": caps.get("serverResolvedCommandContext") is True}


def _action(action, label, payload, confirm=False, subtext=""):
    return {"id": action, "label": label, "payload": payload, "confirm": confirm, "subtext": subtext}


def thread_actions(state, shell, detail, features, now):
    if state.lifecycle in ("archived", "deleted"):
        return []
    actions = []
    req = shell.get("pendingRuntimeRequest")
    req_id = req.get("id") if req else None
    if req_id and live_request(detail, req_id):
        item = request_item(detail, req_id) or {}
        if state.attention == "approval" and item.get("type") == "approval_request":
            for option in item.get("options") or DEFAULT_OPTIONS:
                decision = option.get("decision")
                if decision not in DECISIONS:
                    continue
                actions.append(_action(APPROVE_IDS.get(decision, "deny"), option.get("label") or decision,
                                       {"requestId": req_id, "decision": decision},
                                       decision in ("acceptForSession", "acceptAlways"),
                                       option.get("warning") or ""))
        elif state.attention == "input" and item.get("type") == "user_input_request":
            questions = item.get("questions") or []
            if len(questions) == 1 and not questions[0].get("multiSelect"):
                q = questions[0]
                for option in q.get("options") or []:
                    actions.append(_action("answer", option["label"],
                                           {"requestId": req_id,
                                            "answers": {q["id"]: option.get("value", option["label"])}},
                                           False, option.get("description") or ""))
    if state.attention == "plan":
        plan = active_plan(detail)
        if plan:
            actions.append(_action("implement_plan", "Implement plan", {"planId": plan["id"]}, True))
    if state.status == Status.DONE and features.get("visit"):
        actions.append(_action("acknowledge", "Mark seen", {"completedAt": shell.get("latestRunCompletedAt")}))
    if features.get("settle"):
        if state.lifecycle == "settled":
            actions.append(_action("unsettle", "Reopen", {}))
        elif state.lifecycle == "active" and state.status in (Status.IDLE, Status.DONE, Status.UNKNOWN):
            actions.append(_action("settle", "Settle", {}))
    if features.get("snooze"):
        if state.lifecycle == "snoozed":
            actions.append(_action("unsnooze", "Wake now", {}))
        elif (state.lifecycle == "active" and state.attention not in ("approval", "input")
              and state.activity != "queued"):
            actions.append(_action("snooze", "Snooze 1h", {}))
    return actions


def capabilities(state, shell, actions):
    active = state.lifecycle == "active"
    caps = ["read"]
    if shell.get("activeRunId") and active:
        caps.append("stop")
    if active and state.status in (Status.IDLE, Status.DONE):
        caps.append("continue")
    caps.extend(a["id"] for a in actions)
    return tuple(dict.fromkeys(caps))


def _message(tid, text, features, shell, *, limit=4096, error=MESSAGE_ERROR):
    if not isinstance(text, str) or not 1 <= len(text) <= limit or not text.strip():
        raise ValueError(error)
    msg = {"type": "message.dispatch", "threadId": tid, "messageId": str(uuid.uuid4()),
           "createdBy": "user", "creationSource": "web", "text": text, "attachments": [],
           "dispatchMode": {"type": "start_immediately"}, "deliveryIntent": "auto"}
    if not features.get("resolved_context") and shell.get("activeRunId"):
        msg["dispatchMode"] = {"type": "queue_after_active"}
        del msg["deliveryIntent"]
    return msg


def build_commands(action, payload, shell, detail, features, *, text=None, now_iso):
    tid = shell["id"]
    if action in ("approve", "approve_session", "approve_always", "deny"):
        return [{"type": "runtime-request.respond", "threadId": tid,
                 "requestId": payload["requestId"], "decision": payload["decision"]}]
    if action == "answer":
        return [{"type": "runtime-request.respond", "threadId": tid,
                 "requestId": payload["requestId"], "answers": payload["answers"]}]
    if action == "stop":
        return [{"type": "run.interrupt", "threadId": tid, "runId": shell["activeRunId"], "holdQueue": True}]
    if action == "continue":
        return [_message(tid, text, features, shell)]
    if action == "implement_plan":
        plan = active_plan(detail)
        if not plan or plan["id"] != payload["planId"]:
            raise ValueError("T3 plan is no longer active")
        message = _message(tid, "PLEASE IMPLEMENT THIS PLAN:\n" + plan["markdown"].strip(), features, shell,
                           limit=PLAN_MAX_CHARS, error=PLAN_ERROR)
        message["sourcePlanRef"] = {"threadId": tid, "planId": plan["id"]}
        return [{"type": "thread.interaction-mode.set", "threadId": tid, "interactionMode": "default"}, message]
    if action == "acknowledge":
        return [{"type": "thread.visit", "threadId": tid, "visitedAt": payload["completedAt"]}]
    if action == "settle":
        return [{"type": "thread.settle", "threadId": tid}]
    if action in ("unsettle", "unsnooze"):
        return [{"type": "thread." + action, "threadId": tid, "reason": "user"}]
    if action == "snooze":
        until = datetime.fromtimestamp(timestamp(now_iso) + 3600, UTC)
        return [{"type": "thread.snooze", "threadId": tid,
                 "snoozedUntil": until.strftime("%Y-%m-%dT%H:%M:%S.") + f"{until.microsecond // 1000:03d}Z"}]
    raise ValueError(f"unknown T3 action {action!r}")
