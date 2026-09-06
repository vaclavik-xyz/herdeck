"""T3 HTTP adapter. No provider process, terminal emulation or write retries.

Contract: T3 0.0.31, /api/orchestration/{shell,threads/:id,dispatch}.
The connector callback surface matches Herdeck's existing bridge connector.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import uuid
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .model import AgentKey, AgentState, Status


class T3Error(Exception):
    """A sanitized transport/contract error; never includes response bodies."""


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
            raise T3Error(f"T3 HTTP {exc.code}") from None
        except (URLError, OSError, ValueError):
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
            if payload.get("responseMode") != "message" and payload.get("requestType") not in (
                "tool_user_input", "auth_tokens_refresh"
            ):
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
    if p["kind"] == "approval.requested":
        options = p.get("options") or [
            {"decision": "accept", "label": "Approve"},
            {"decision": "decline", "label": "Deny"},
        ]
        for opt in options:
            if opt.get("decision") not in ("accept", "decline", "cancel"):
                continue
            actions.append({"id": "approve" if opt["decision"] == "accept" else "deny",
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


def thread_state(server_id, thread, projects, epoch):
    session = thread.get("session") or {}
    pending = pending_requests(thread)
    state = session.get("status")
    if pending or thread.get("hasPendingApprovals") or thread.get("hasPendingUserInput"):
        status = Status.BLOCKED
    elif state in ("running", "starting") or session.get("activeTurnId"):
        status = Status.WORKING
    elif state in (None, "idle", "ready", "interrupted", "stopped"):
        status = Status.IDLE
    else:
        status = Status.UNKNOWN
    actions = _actions(pending)
    capabilities = ["read"]
    if session.get("activeTurnId"):
        capabilities.append("stop")
    if status == Status.IDLE and thread.get("runtimeMode") and thread.get("interactionMode"):
        capabilities.append("continue")
    capabilities.extend(a["id"] for a in actions)
    # Message streaming does not change an action's identity. A new user message,
    # turn, request or mode does. Epoch invalidates all pre-reconnect controls.
    identity = [epoch, thread["id"], session, pending, thread.get("latestTurn"),
                thread.get("runtimeMode"), thread.get("interactionMode"), thread.get("modelSelection"),
                [m.get("id") for m in thread.get("messages", []) if m.get("role") == "user"]]
    revision = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    project = projects.get(thread.get("projectId"), {})
    preview = "\n".join(str(p.get("detail") or p.get("questions") or "Approval requested") for p in pending)
    if not preview:
        messages = thread.get("messages", [])
        preview = str(messages[-1].get("text", "")) if messages else ""
    return AgentState(AgentKey(server_id, thread["id"]),
        session.get("providerName") or (thread.get("modelSelection") or {}).get("provider", "default"),
        thread.get("title", "T3 conversation"), status,
        project=project.get("title", ""), repo=project.get("workspaceRoot", ""),
        branch=thread.get("branch") or "", title=thread.get("title", ""),
        display_agent="T3 Code", backend="t3", capabilities=tuple(capabilities),
        backend_revision=revision, backend_actions=actions, preview=preview[-12000:])


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
        self._uncertain = set()
        self._lock = asyncio.Lock()
        self._stop = False
        self.last_connect_error = None

    def stop(self):
        self._stop = True

    async def refresh(self):
        shell = await asyncio.to_thread(self.http.get, "/api/orchestration/shell")
        if not isinstance(shell.get("threads"), list) or not isinstance(shell.get("projects"), list):
            raise T3Error("Unsupported T3 shell contract")
        projects = {p["id"]: p for p in shell["projects"]}
        threads, states = {}, {}
        for summary in shell["threads"]:
            if summary.get("archivedAt"):
                continue
            tid = summary["id"]
            detail = await asyncio.to_thread(self.http.get,
                "/api/orchestration/threads/" + quote(tid, safe=""))
            t = {**summary, **detail["thread"]}
            threads[tid] = t
            states[tid] = thread_state(self.server.id, t, projects, self._epoch)
            if tid in self._uncertain:
                states[tid].capabilities = ("read",)
                states[tid].backend_actions = []
                states[tid].progress = "Delivery uncertain — inspect T3"
                states[tid].preview = "Delivery uncertain. Inspect T3 before reconnecting Herdeck.\n" + states[tid].preview
        self._threads, self.states = threads, states
        self._consumed.intersection_update(s.backend_revision for s in states.values())
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
                online = False
                self._on_connection(self.server.id, False)
            await asyncio.sleep(1)
        self._on_connection(self.server.id, False)

    async def send(self, msg):
        async with self._lock:
            req, tid = msg.get("req"), msg.get("pane_id")
            try:
                await self.refresh()
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
                    command.update(type="thread.user-input.respond" if action == "answer" else "thread.approval.respond",
                                   **match["payload"])
                self._consumed.add(state.backend_revision)
                try:
                    await asyncio.to_thread(self.http.dispatch, command)
                except T3Error:
                    self._uncertain.add(tid)
                    self._on_result(req, {"ok": False, "uncertain": True,
                        "message": "T3 delivery uncertain; inspect the conversation before retrying"})
                    return
                self._on_result(req, {"ok": True, "sent": True, "accepted": True})
            except (T3Error, KeyError, TypeError, ValueError) as exc:
                message = str(exc) if isinstance(exc, T3Error) else "Unsupported T3 contract"
                self._on_result(req, {"ok": False, "skipped": True, "message": message})
