"""Runtime side of the subagent-hook installer: ``/maintenance/servers/{id}/hooks``.

The hooks live on the agents' Mac, next to the bridge, so the runtime relays
to that bridge's ``hooks`` message (``herdeck/hooks_install.py``; bridge
capability ``hooks``, full token only):

* ``GET /maintenance/servers/{id}/hooks`` -> ``{"action": "status"}``
* ``POST /maintenance/servers/{id}/hooks`` ``{"action": "install" |
  "uninstall" | "status", "agents": ["claude", "codex"]}`` (agents optional)

Both answer::

    {"ok": bool, "code": "ok" | "failed" | "readonly" | "unsupported" |
     "disconnected" | "timeout", "message": str, "server_id": str,
     "agents": {agent: {...full per-agent status...}} | null}

``code`` is ``failed`` when the bridge answered but an agent's file could not
be used (its ``error`` says why). GET /maintenance carries the compact
``hooks`` summary per server (``hooks_install.summary``), asked once per
connection right after the first snapshot and refreshed by every answer.

``LiveSource`` mixes in ``HooksMixin``; its connector callbacks route through
the ``_hooks_on_*`` hooks and a hooks request id never reaches the deck's own
result handling.
"""

from __future__ import annotations

import itertools
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import unquote

from ..hooks_install import ACTIONS, AGENTS, CAPABILITY, summary

HOOKS_WAIT_S = 14.0  # the bridge caps its own work at 10 s
_ROUTE_RE = re.compile(r"^/maintenance/servers/([^/]+)/hooks$")


def route_server_id(path: str) -> str | None:
    match = _ROUTE_RE.fullmatch(path)
    if match is None:
        return None
    return unquote(match.group(1)) or None


@dataclass
class _Wait:
    server_id: str
    event: threading.Event = field(default_factory=threading.Event)
    data: dict | None = None
    error: str | None = None
    on_done: Callable[[_Wait], None] | None = None


def _outcome(code: str, message: str, server_id: str, agents: dict | None = None) -> dict:
    return {
        "ok": code == "ok",
        "code": code,
        "message": message,
        "server_id": server_id,
        "agents": agents,
    }


class HooksMixin:
    """Hooks relay for ``LiveSource`` (uses its ``_servers``, ``_runners``,
    ``_connected`` and ``_lock``)."""

    def _hooks_init(self) -> None:
        self._hooks_lock = threading.Lock()
        self._hooks_waits: dict[str, _Wait] = {}
        self._hooks_summary: dict[str, dict] = {}
        self._hooks_asked: set[str] = set()
        self._hooks_reqs = itertools.count(1)

    # --- connector hooks ---------------------------------------------------
    def _hooks_finish(self, req: str | None, data: object, error: str | None) -> bool:
        if req is None:
            return False
        with self._hooks_lock:
            wait = self._hooks_waits.pop(req, None)
        if wait is None:
            return False
        if error is None and isinstance(data, dict) and isinstance(data.get("agents"), dict):
            wait.data = data
            with self._hooks_lock:
                self._hooks_summary[wait.server_id] = summary(data["agents"])
        else:
            wait.error = error or "malformed hooks reply"
        wait.event.set()
        if wait.on_done is not None:
            wait.on_done(wait)
        return True

    def _hooks_on_result(self, req: str | None, data: object) -> bool:
        return self._hooks_finish(req, data, None)

    def _hooks_on_error(self, req: str | None, message: str) -> bool:
        return self._hooks_finish(req, None, message or "bridge error")

    def _hooks_on_connection(self, server_id: str, up: bool) -> None:
        if up:
            return
        with self._hooks_lock:
            self._hooks_summary.pop(server_id, None)
            self._hooks_asked.discard(server_id)
            victims = [r for r, w in self._hooks_waits.items() if w.server_id == server_id]
            waits = [self._hooks_waits.pop(r) for r in victims]
        for wait in waits:
            wait.error = "disconnected"
            wait.event.set()

    def _hooks_on_snapshot(self, server_id: str) -> None:
        """Ask a ``hooks``-capable bridge for its status once per connection."""
        runner = self._runners.get(server_id)
        capabilities = getattr(getattr(runner, "connector", None), "capabilities", frozenset())
        if runner is None or CAPABILITY not in capabilities:
            return
        with self._hooks_lock:
            if server_id in self._hooks_asked:
                return
            self._hooks_asked.add(server_id)
        self._hooks_send(server_id, runner, {"action": "status"})

    def _hooks_send(self, server_id: str, runner, body: dict) -> tuple[str, _Wait]:
        req = f"hk{next(self._hooks_reqs)}"
        wait = _Wait(server_id)
        with self._hooks_lock:
            self._hooks_waits[req] = wait
        runner.send({"type": "hooks", "req": req, **body})
        return req, wait

    # --- operations ----------------------------------------------------------
    def hooks_summary(self, server_id: str) -> dict | None:
        with self._hooks_lock:
            return self._hooks_summary.get(server_id)

    def bridge_hooks(
        self, server_id: str, action: str, agents: list[str] | None, *, wait_s: float = HOOKS_WAIT_S
    ) -> dict | None:
        """Relay one hooks request to ``server_id``'s bridge. None = unknown server."""
        if server_id not in self._servers:
            return None
        runner = self._runners.get(server_id)
        with self._lock:
            connected = bool(self._connected.get(server_id))
        if runner is None or not connected:
            return _outcome("disconnected", "the server is not connected", server_id)
        capabilities = getattr(getattr(runner, "connector", None), "capabilities", frozenset())
        if CAPABILITY not in capabilities:
            return _outcome(
                "unsupported",
                "this bridge cannot install subagent hooks (herdeck before hook install)",
                server_id,
            )
        body: dict = {"action": action}
        if agents is not None:
            body["agents"] = agents
        req, wait = self._hooks_send(server_id, runner, body)
        if not wait.event.wait(wait_s):
            with self._hooks_lock:
                self._hooks_waits.pop(req, None)
            return _outcome("timeout", "the bridge did not answer in time", server_id)
        if wait.data is None:
            message = wait.error or "failed"
            if message == "disconnected":
                return _outcome("disconnected", "the bridge disconnected", server_id)
            code = "readonly" if message.startswith("read-only token") else "failed"
            return _outcome(code, message, server_id)
        agents_out = wait.data["agents"]
        errors = [a.get("error") for a in agents_out.values() if isinstance(a, dict) and a.get("error")]
        if errors:
            return _outcome("failed", "; ".join(errors), server_id, agents_out)
        return _outcome("ok", "", server_id, agents_out)


# --- HTTP route helpers (called from DeckApp's handler) ----------------------


def _parse_agents(raw: object) -> list[str] | None | bool:
    """A valid agent list, None (not given = all), or False (invalid)."""
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw or not all(a in AGENTS for a in raw):
        return False
    return list(dict.fromkeys(raw))


def handle_get(source, path: str) -> tuple[int, dict | None]:
    """GET /maintenance/servers/{id}/hooks."""
    server_id = route_server_id(path)
    if server_id is None or not callable(getattr(source, "bridge_hooks", None)):
        return 404, None
    result = source.bridge_hooks(server_id, "status", None)
    return (200, result) if result is not None else (404, None)


def handle_post(source, path: str, body: dict) -> tuple[int, dict | None]:
    """POST /maintenance/servers/{id}/hooks {"action", "agents"?}."""
    server_id = route_server_id(path)
    if server_id is None or not callable(getattr(source, "bridge_hooks", None)):
        return 404, None
    action = body.get("action")
    agents = _parse_agents(body.get("agents"))
    if action not in ACTIONS or agents is False:
        return 400, None
    result = source.bridge_hooks(server_id, action, agents)
    return (200, result) if result is not None else (404, None)
