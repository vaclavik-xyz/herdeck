"""Runtime side of the usage agent installer: ``/maintenance/servers/{id}/usage-agent``.

The usage agent runs on the bridge's machine, in its user's login session, so
the runtime relays to that bridge's ``usage_agent`` message
(``herdeck/usage_agent_install.py``; bridge capability ``usage_agent``, full
token only) — the same pattern as the subagent hooks (hooks_relay.py):

* ``GET /maintenance/servers/{id}/usage-agent`` -> ``{"action": "status"}``
* ``POST /maintenance/servers/{id}/usage-agent`` ``{"action": "install" |
  "uninstall" | "status"}``

Both answer::

    {"ok": bool, "code": "ok" | "failed" | "no_gui_session" | "readonly" |
     "unsupported" | "disconnected" | "timeout", "message": str,
     "server_id": str, "agent": {...the bridge's reply data...} | null}

``agent`` is the bridge's reply ``data`` (``installed``, ``running``,
``fresh``, ``file_age_s``, ``providers``, ``gui_session``, ``bridge_usage``, ...;
see usage_agent_install.py) whenever the bridge answered, also on a failed
install. GET /maintenance carries the compact :func:`summary` per server as
``usage_agent``, asked once per connection right after the first snapshot and
refreshed by every answer (so ``file_age_s`` there is as old as that answer;
GET the route for a live one).

``LiveSource`` mixes in ``UsageAgentMixin``; its connector callbacks route
through the ``_usage_agent_on_*`` hooks, so a request id of this relay never
reaches the deck's own result handling.
"""

from __future__ import annotations

import itertools
import re
import threading
from dataclasses import dataclass, field
from urllib.parse import unquote

from ..usage_agent_install import ACTIONS, CAPABILITY

USAGE_AGENT_WAIT_S = 34.0  # the bridge caps its own work at 30 s
_ROUTE_RE = re.compile(r"^/maintenance/servers/([^/]+)/usage-agent$")
_SUMMARY_KEYS = ("installed", "running", "fresh", "bridge_usage")


def route_server_id(path: str) -> str | None:
    match = _ROUTE_RE.fullmatch(path)
    if match is None:
        return None
    return unquote(match.group(1)) or None


def summary(data: dict) -> dict:
    """The compact view GET /maintenance carries."""
    out: dict = {key: data.get(key) is True for key in _SUMMARY_KEYS}
    age = data.get("file_age_s")
    out["file_age_s"] = age if isinstance(age, (int, float)) and not isinstance(age, bool) else None
    providers = data.get("providers")
    out["providers"] = [p for p in providers if isinstance(p, str)] if isinstance(providers, list) else []
    error = data.get("error")
    out["error"] = error if isinstance(error, str) else None
    return out


@dataclass
class _Wait:
    server_id: str
    event: threading.Event = field(default_factory=threading.Event)
    data: dict | None = None
    error: str | None = None


def _outcome(code: str, message: str, server_id: str, agent: dict | None = None) -> dict:
    return {
        "ok": code == "ok",
        "code": code,
        "message": message,
        "server_id": server_id,
        "agent": agent,
    }


class UsageAgentMixin:
    """Usage agent relay for ``LiveSource`` (uses its ``_servers``,
    ``_runners``, ``_connected`` and ``_lock``)."""

    def _usage_agent_init(self) -> None:
        self._ua_lock = threading.Lock()
        self._ua_waits: dict[str, _Wait] = {}
        self._ua_summary: dict[str, dict] = {}
        self._ua_asked: set[str] = set()
        self._ua_reqs = itertools.count(1)

    # --- connector hooks ---------------------------------------------------
    def _usage_agent_finish(self, req: str | None, data: object, error: str | None) -> bool:
        if req is None:
            return False
        with self._ua_lock:
            wait = self._ua_waits.pop(req, None)
        if wait is None:
            return False
        if error is None and isinstance(data, dict) and isinstance(data.get("ok"), bool):
            wait.data = data
            with self._ua_lock:
                self._ua_summary[wait.server_id] = summary(data)
        else:
            wait.error = error or "malformed usage_agent reply"
        wait.event.set()
        return True

    def _usage_agent_on_result(self, req: str | None, data: object) -> bool:
        return self._usage_agent_finish(req, data, None)

    def _usage_agent_on_error(self, req: str | None, message: str) -> bool:
        return self._usage_agent_finish(req, None, message or "bridge error")

    def _usage_agent_on_connection(self, server_id: str, up: bool) -> None:
        if up:
            return
        with self._ua_lock:
            self._ua_summary.pop(server_id, None)
            self._ua_asked.discard(server_id)
            victims = [r for r, w in self._ua_waits.items() if w.server_id == server_id]
            waits = [self._ua_waits.pop(r) for r in victims]
        for wait in waits:
            wait.error = "disconnected"
            wait.event.set()

    def _usage_agent_capable(self, runner) -> bool:
        capabilities = getattr(getattr(runner, "connector", None), "capabilities", frozenset())
        return CAPABILITY in capabilities

    def _usage_agent_on_snapshot(self, server_id: str) -> None:
        """Ask a ``usage_agent``-capable bridge for its status once per connection."""
        runner = self._runners.get(server_id)
        if runner is None or not self._usage_agent_capable(runner):
            return
        with self._ua_lock:
            if server_id in self._ua_asked:
                return
            self._ua_asked.add(server_id)
        self._usage_agent_send(server_id, runner, "status")

    def _usage_agent_send(self, server_id: str, runner, action: str) -> tuple[str, _Wait]:
        req = f"ua{next(self._ua_reqs)}"
        wait = _Wait(server_id)
        with self._ua_lock:
            self._ua_waits[req] = wait
        runner.send({"type": "usage_agent", "req": req, "action": action})
        return req, wait

    # --- operations ----------------------------------------------------------
    def usage_agent_summary(self, server_id: str) -> dict | None:
        with self._ua_lock:
            return self._ua_summary.get(server_id)

    def bridge_usage_agent(
        self, server_id: str, action: str, *, wait_s: float = USAGE_AGENT_WAIT_S
    ) -> dict | None:
        """Relay one usage_agent request to ``server_id``'s bridge. None = unknown server."""
        if server_id not in self._servers:
            return None
        runner = self._runners.get(server_id)
        with self._lock:
            connected = bool(self._connected.get(server_id))
        if runner is None or not connected:
            return _outcome("disconnected", "the server is not connected", server_id)
        if not self._usage_agent_capable(runner):
            return _outcome(
                "unsupported",
                "this bridge cannot install the usage agent (herdeck before the usage agent)",
                server_id,
            )
        req, wait = self._usage_agent_send(server_id, runner, action)
        if not wait.event.wait(wait_s):
            with self._ua_lock:
                self._ua_waits.pop(req, None)
            return _outcome("timeout", "the bridge did not answer in time", server_id)
        if wait.data is None:
            message = wait.error or "failed"
            if message == "disconnected":
                return _outcome("disconnected", "the bridge disconnected", server_id)
            code = "readonly" if message.startswith("read-only token") else "failed"
            return _outcome(code, message, server_id)
        data = wait.data
        if data["ok"]:
            return _outcome("ok", "", server_id, data)
        code = data.get("code") if isinstance(data.get("code"), str) else "failed"
        message = data.get("error") if isinstance(data.get("error"), str) else "failed"
        return _outcome(code if code != "ok" else "failed", message, server_id, data)


# --- HTTP route helpers (called from DeckApp's handler) ----------------------


def handle_get(source, path: str) -> tuple[int, dict | None]:
    """GET /maintenance/servers/{id}/usage-agent."""
    server_id = route_server_id(path)
    if server_id is None or not callable(getattr(source, "bridge_usage_agent", None)):
        return 404, None
    result = source.bridge_usage_agent(server_id, "status")
    return (200, result) if result is not None else (404, None)


def handle_post(source, path: str, body: dict) -> tuple[int, dict | None]:
    """POST /maintenance/servers/{id}/usage-agent {"action"}."""
    server_id = route_server_id(path)
    if server_id is None or not callable(getattr(source, "bridge_usage_agent", None)):
        return 404, None
    action = body.get("action")
    if action not in ACTIONS:
        return 400, None
    result = source.bridge_usage_agent(server_id, action)
    return (200, result) if result is not None else (404, None)
