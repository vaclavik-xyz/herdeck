"""T3 HTTP adapter. No provider process, terminal emulation or write retries.

Contract: T3 0.0.31 core, negotiated 0.0.38 lifecycle, /api/orchestration/{shell,threads/:id,dispatch}.
The connector callback surface matches Herdeck's existing bridge connector.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import time
import uuid
from datetime import UTC, datetime
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .model import AgentKey, AgentState, Status
from .t3_actions import extend_actions, semantic_command
from .t3_seen import SeenStore
from .t3_state import lifecycle, queued_start, timestamp


class T3Error(Exception):
    """A sanitized transport/contract error; never includes response bodies."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class T3Http:
    def __init__(self, url: str, token: str):
        p = urlsplit(url)
        try:
            ip = ipaddress.ip_address(p.hostname or "")
            private = ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10")
        except ValueError:
            private = p.hostname == "localhost"
        if (p.scheme not in ("http", "https") or not p.hostname or p.username or p.password
                or p.query or p.fragment or p.path not in ("", "/")
                or (p.scheme == "http" and not private)):
            raise ValueError("T3 requires a clean HTTPS origin or loopback/Tailscale HTTP origin")
        self.url = url.rstrip("/")
        self._token = token
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def _request(self, path, data=None):
        req = Request(self.url + path, data=json.dumps(data).encode() if data is not None else None,
                      headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"})
        try:
            with self._opener.open(req, timeout=8) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise T3Error("T3 response exceeds limit")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise T3Error("Unsupported T3 response")
                return result
        except HTTPError as exc:
            raise T3Error(f"T3 HTTP {exc.code}", exc.code) from None
        except (URLError, OSError, ValueError, HTTPException):
            raise T3Error("T3 transport or response error") from None

    def get(self, path):
        return self._request(path)

    def dispatch(self, command):
        return self._request("/api/orchestration/dispatch", command)


def pending_requests(thread):
    pending = {}
    for activity in thread.get("activities", []):
        payload = activity.get("payload") or {}
        rid = payload.get("requestId")
        kind = activity.get("kind", "")
        if not rid:
            continue
        if kind in ("approval.requested", "user-input.requested"):
            pending[rid] = {**payload, "kind": kind}
        elif kind in ("approval.resolved", "user-input.resolved") or (
            kind.endswith(".failed") and any(s in str(payload.get("detail", "")).lower()
                for s in ("stale pending", "unknown pending"))
        ):
            pending.pop(rid, None)
    return list(pending.values())


def _actions(pending):
    actions = []
    # Multiple requests remain individually identified. Only offer choices for
    # the first pending request; the next appears after its resolution.
    if not pending:
        return actions
    p = pending[0]
    if p.get("responseMode") == "message" or p.get("requestType") in ("tool_user_input", "auth_tokens_refresh"):
        return actions
    if p["kind"] == "approval.requested":
        options = p.get("options") or [
            {"decision": "accept", "label": "Approve"},
            {"decision": "decline", "label": "Deny"},
        ]
        for opt in options:
            if opt.get("decision") not in ("accept", "acceptForSession", "acceptAlways", "decline", "cancel"):
                continue
            actions.append({"id": {"accept": "approve", "acceptForSession": "approve_session", "acceptAlways": "approve_always"}.get(opt["decision"], "deny"),
                "confirm": opt["decision"] in ("acceptForSession", "acceptAlways"),
                "label": opt["label"], "subtext": opt.get("warning", ""),
                "payload": {"requestId": p["requestId"], "decision": opt["decision"]}})
    else:
        questions = p.get("questions", [])
        # Hardware v1 supports a single-choice question. Complex/free-text forms
        # remain visible in the preview and must be answered in T3.
        if len(questions) == 1 and not questions[0].get("multiSelect"):
            q = questions[0]
            for opt in q.get("options", []):
                actions.append({"id": "answer", "label": opt["label"],
                    "subtext": opt.get("description", ""),
                    "payload": {"requestId": p["requestId"],
                                "answers": {q["id"]: opt.get("value", opt["label"])}}})
    return actions


def thread_state(server_id, thread, projects, epoch, *, now=None, acknowledged=None, features=None, done_ttl=0):
    now = datetime.now(UTC).timestamp() if now is None else now
    session = thread.get("session") or {}
    latest_turn = thread.get("latestTurn") or {}
    pending = pending_requests(thread)
    state = session.get("status")
    life = lifecycle(thread, pending, now)
    activity, attention, label = state or "idle", "", ""
    completed = latest_turn.get("completedAt") or ""
    completed_time = timestamp(completed)
    # Optional temporary display policy, not a shared T3 read receipt.
    completion_expired = done_ttl > 0 and completed_time is not None and now - completed_time >= done_ttl
    if life != "active":
        status, label = Status.IDLE, life.upper()
    elif pending or thread.get("hasPendingApprovals") or thread.get("hasPendingUserInput"):
        status = Status.BLOCKED
        attention = "approval" if any(p["kind"] == "approval.requested" for p in pending) or thread.get("hasPendingApprovals") else "input"
    elif state == "error":
        status, attention, label = Status.UNKNOWN, "error", "ERROR"
    elif state in ("running", "starting") or session.get("activeTurnId"):
        status = Status.WORKING
        label = "CONNECTING" if state == "starting" else ""
    elif queued_start(thread, now):
        status, activity, label = Status.WORKING, "queued", "QUEUED"
    elif state == "error" or latest_turn.get("state") == "error":
        status, attention, label = Status.UNKNOWN, "error", "ERROR"
    elif thread.get("interactionMode") == "plan" and thread.get("hasActionableProposedPlan"):
        status, attention, label = Status.BLOCKED, "plan", "PLAN READY"
    elif state in (None, "idle", "ready", "interrupted", "stopped"):
        activity = thread.get("backgroundLiveness") or activity
        if activity == "working":
            status = Status.WORKING
        elif activity == "monitoring":
            status, label = Status.WAITING, "MONITORING"
        else:
            status = (Status.DONE if state != "interrupted" and latest_turn.get("state") == "completed"
                      and completed and completed != acknowledged and not completion_expired else Status.IDLE)
            attention = "completion" if status == Status.DONE else ""
    else:
        status = Status.UNKNOWN
    actions = _actions(pending) if life == "active" else []
    capabilities = ["read"]
    if session.get("activeTurnId") and life == "active":
        capabilities.append("stop")
    if life == "active" and status in (Status.IDLE, Status.DONE) and thread.get("runtimeMode") and thread.get("interactionMode"):
        capabilities.append("continue")
    extend_actions(actions, thread, life, status, attention, activity, features or {}, now, completed)
    capabilities.extend(a["id"] for a in actions)
    if features is not None and not features.get("core"):
        capabilities, actions = ["read"], []
    identity = [epoch, thread["id"], session, pending, latest_turn,
                thread.get("runtimeMode"), thread.get("interactionMode"), thread.get("modelSelection"),
                [m.get("id") for m in thread.get("messages", []) if m.get("role") == "user"],
                {k: thread.get(k) for k in ("settledOverride", "settledAt", "snoozedUntil", "snoozedAt",
                    "archivedAt", "deletedAt", "latestUserMessageAt", "hasActionableProposedPlan", "proposedPlans", "backgroundLiveness")}, life, acknowledged, features or {}, status.value]
    revision = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    project = projects.get(thread.get("projectId"), {})
    preview = "\n".join(str(p.get("detail") or p.get("questions") or "Approval requested") for p in pending)
    if not preview:
        messages = thread.get("messages", [])
        preview = str(messages[-1].get("text", "")) if messages else ""
    if attention == "error":
        preview = "T3 error: " + str(session.get("lastError") or "The latest turn failed. Inspect T3 for details.") + "\n" + preview
    elif attention == "plan":
        plans = thread.get("proposedPlans") or []
        preview = "Plan ready — choose how to implement it.\n" + (str(plans[-1].get("planMarkdown", "")) if plans else preview)
    elif attention in ("input", "approval") and not _actions(pending):
        preview = "Answer in T3 — this form needs the full conversation UI.\n" + preview
    elif activity in ("working", "monitoring") and not session.get("activeTurnId"):
        preview = "Background work is active. Stop session ends the provider session; individual tasks are managed in T3.\n" + preview
    if features is not None and not features.get("core"):
        preview = "Unsupported T3 server version. Read-only connection.\n" + preview
    if life != "active":
        preview = life.capitalize() + " thread.\n" + preview
    return AgentState(AgentKey(server_id, thread["id"]),
        session.get("providerName") or (thread.get("modelSelection") or {}).get("provider", "default"),
        thread.get("title", "T3 conversation"), status,
        project=project.get("title", ""), repo=project.get("workspaceRoot", ""),
        branch=thread.get("branch") or "", title=thread.get("title", ""),
        display_agent="T3 Code", backend="t3", capabilities=tuple(capabilities),
        backend_revision=revision, backend_actions=actions, preview=preview[:12000],
        lifecycle=life, activity=activity, attention=attention, completed_at=completed,
        state_labels={status.value: label} if label else {})


def _observed_effect(thread, command):
    """Reconcile an uncertain write using its exact message/turn/request identity.

    A timestamp change or reconnect alone is insufficient evidence to unlock it.
    """
    kind = command["type"]
    if kind == "thread.turn.start":
        return any(m.get("id") == command["message"]["messageId"]
                   for m in thread.get("messages", []))
    if kind == "thread.turn.interrupt":
        return (thread.get("session") or {}).get("activeTurnId") != command["turnId"]
    if kind == "thread.settle":
        return thread.get("settledOverride") == "settled"
    if kind == "thread.unsettle":
        return thread.get("settledOverride") == "active"
    if kind == "thread.snooze":
        actual, expected = timestamp(thread.get("snoozedUntil")), timestamp(command["snoozedUntil"])
        return actual is not None and expected is not None and actual == expected
    if kind == "thread.unsnooze":
        return thread.get("snoozedUntil") is None
    if kind == "thread.session.stop":
        return (thread.get("session") or {}).get("status") == "stopped"
    return "requestId" in command and not any(p["requestId"] == command["requestId"] for p in pending_requests(thread))


class T3Connector:
    protocol = 1
    capabilities = frozenset()

    def __init__(self, server, on_snapshot, on_event, on_connection, on_result=None,
                 on_error=None, **kwargs):
        self.server = server
        self.http = T3Http(server.url, server.token)
        self._on_snapshot = on_snapshot
        self._on_connection = on_connection
        self._on_result = on_result or (lambda *args: None)
        self._on_error = on_error or (lambda *args: None)
        self.states = {}
        self._threads = {}
        self._epoch = uuid.uuid4().hex
        self._consumed = set()
        self._uncertain = {}
        self._lock = asyncio.Lock()
        self._stop = False
        self.last_connect_error = None
        self._features = None
        self._done_ttl = max(0, int(os.environ.get("HERDECK_T3_DONE_TTL_SECONDS", "0")))
        self._cache = {}
        config = Path(os.environ.get("HERDECK_CONFIG", str(Path.home() / ".config/herdeck/config.toml")))
        self._seen = kwargs.get("seen_store") or SeenStore(config.parent / "t3-seen", server.id)

    def stop(self):
        self._stop = True

    async def refresh(self, force=None):
        if self._features is None:
            descriptor = await asyncio.to_thread(self.http.get, "/.well-known/t3/environment")
            from .t3_actions import negotiated_features
            self._features = negotiated_features(descriptor)
        shell = await asyncio.to_thread(self.http.get, "/api/orchestration/shell")
        if not isinstance(shell.get("threads"), list) or not isinstance(shell.get("projects"), list):
            raise T3Error("Unsupported T3 shell contract")
        projects = {p["id"]: p for p in shell["projects"]}
        threads, states = {}, {}
        now = time.monotonic()
        for summary in shell["threads"]:
            tid = summary["id"]
            signature = json.dumps(summary, sort_keys=True)
            cached = self._cache.get(tid)
            stale = False
            try:
                if force == tid or not cached or cached[0] != signature or now - cached[1] >= 15:
                    if summary.get("deletedAt") or summary.get("archivedAt"):
                        detail = {}
                    else:
                        response = await asyncio.to_thread(self.http.get,
                            "/api/orchestration/threads/" + quote(tid, safe="") + "?turnLimit=3")
                        detail = response["thread"]
                        if not isinstance(detail, dict) or detail.get("id") != tid:
                            raise T3Error("Unsupported T3 thread contract")
                    self._cache[tid] = (signature, now, detail)
                else:
                    detail = cached[2]
            except T3Error as exc:
                if exc.code in (401, 403):
                    raise
                if exc.code == 404:
                    self._cache.pop(tid, None)
                    continue
                detail, stale = (cached[2] if cached else {}), True
            except (KeyError, TypeError, ValueError):
                detail, stale = (cached[2] if cached else {}), True
            # Summary is fresher than cached detail and owns lifecycle/attention.
            t = {**detail, **summary}
            threads[tid] = t
            states[tid] = thread_state(self.server.id, t, projects, self._epoch,
                acknowledged=self._seen.get(tid), features=self._features, done_ttl=self._done_ttl)
            uncertain = self._uncertain.get(tid)
            if uncertain and not stale and _observed_effect(t, uncertain):
                self._uncertain.pop(tid)
            if stale or tid in self._uncertain:
                s = states[tid]
                s.capabilities, s.backend_actions = ("read",), []
                s.backend_revision += ":stale" if stale else ":uncertain"
                s.progress = "Stale detail" if stale else "Delivery uncertain — inspect T3"
                s.preview = s.progress + ". Controls disabled. Inspect T3.\n" + s.preview
                if stale:
                    s.state_labels = {s.status.value: "STALE"}
        self._cache = {k: v for k, v in self._cache.items() if k in threads}
        self._threads, self.states = threads, states
        self._on_snapshot(self.server.id, list(states.values()))

    async def run(self):
        online = False
        while not self._stop:
            try:
                async with self._lock:
                    await self.refresh()
                self.last_connect_error = None
                self._on_connection(self.server.id, True)
                online = True
            except (T3Error, KeyError, TypeError, ValueError):
                self.last_connect_error = "T3 unavailable or incompatible; check connection and credential"
                if online:
                    self._epoch = uuid.uuid4().hex
                    self._features = None
                    self._cache.clear()
                online = False
                self._on_connection(self.server.id, False)
            await asyncio.sleep(1)
        self._on_connection(self.server.id, False)

    async def send(self, msg):
        async with self._lock:
            req, tid = msg.get("req"), msg.get("pane_id")
            try:
                await self.refresh(force=tid)
                if msg["type"] == "list":
                    return
                state = self.states.get(tid)
                if state is None:
                    raise T3Error("T3 conversation is no longer available")
                if msg["type"] == "read":
                    self._on_result(req, {"pane_id": tid, "text": state.preview})
                    return
                if msg["type"] != "backend_action":
                    raise T3Error("Unsupported T3 action; use semantic controls")
                if msg.get("revision") != state.backend_revision or state.backend_revision in self._consumed:
                    raise T3Error("Stale or already submitted T3 action")
                action = msg.get("action")
                if action not in state.capabilities:
                    raise T3Error("T3 action is not available")
                t = self._threads[tid]
                command = {"commandId": uuid.uuid4().hex, "threadId": tid,
                           "createdAt": datetime.now(UTC).isoformat()}
                if action == "stop":
                    command.update(type="thread.turn.interrupt", turnId=t["session"]["activeTurnId"])
                elif action == "continue":
                    text = msg.get("text")
                    if not isinstance(text, str) or not text.strip() or len(text) > 4096:
                        raise T3Error("T3 message must contain 1–4096 characters")
                    command.update(type="thread.turn.start", runtimeMode=t["runtimeMode"],
                        interactionMode=t["interactionMode"], modelSelection=t["modelSelection"],
                        message={"messageId": uuid.uuid4().hex, "role": "user", "text": text, "attachments": []})
                else:
                    match = next((a for a in state.backend_actions
                        if a["id"] == action and a["payload"] == msg.get("payload")), None)
                    if match is None:
                        raise T3Error("T3 request or answer is stale")
                    if action == "acknowledge":
                        self._seen.mark(tid, state.completed_at)
                        await self.refresh(force=tid)
                        self._on_result(req, {"ok": True, "accepted": True, "local": True})
                        return
                    command.update(semantic_command(action, match["payload"], t))
                self._consumed.add(state.backend_revision)
                try:
                    await asyncio.to_thread(self.http.dispatch, command)
                except T3Error:
                    self._uncertain[tid] = command
                    self._on_result(req, {"ok": False, "uncertain": True,
                        "message": "T3 delivery uncertain; inspect the conversation before retrying"})
                    return
                self._on_result(req, {"ok": True, "sent": True, "accepted": True})
            except (T3Error, KeyError, TypeError, ValueError) as exc:
                message = str(exc) if isinstance(exc, T3Error) else "Unsupported T3 contract"
                self._on_result(req, {"ok": False, "skipped": True, "message": message})
