"""Runtime side of the bridge shared settings editor: ``POST /bridge-settings/{id}``.

The editor sends ``{"base_revision": int, "settings": {...}}``; the runtime
relays it as a ``settings_put`` frame to that bridge (capability ``settings``,
full token only) and waits for the ``result``. HTTP mapping:

* ok -> 200 ``{"ok": true, "revision": N}``
* ``stale_revision`` -> 409, ``invalid`` / ``too_large`` -> 422
  (``{"ok": false, "error", "messages", "revision"}``)
* server not connected or bridge without ``settings`` -> 503
* no reply in ``SETTINGS_WAIT_S`` -> 504
* an error frame from the bridge (read-only token, write failed) -> 502
* unknown server -> 404, malformed body -> 400

``LiveSource`` mixes in ``SettingsRelayMixin``; a settings request id never
reaches the deck's own result handling.
"""

from __future__ import annotations

import itertools
import re
import threading
from dataclasses import dataclass, field
from urllib.parse import unquote

from ..bridge_settings import SETTINGS_CAPABILITY

SETTINGS_WAIT_S = 10.0
_ROUTE_RE = re.compile(r"^/bridge-settings/([^/]+)$")
# Not "st": stats.py owns that prefix (ids hk, ua, st, u, p, r, t are taken).
_REQ_PREFIX = "sp"
_HTTP = {"stale_revision": 409, "invalid": 422, "too_large": 422}


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


def _fail(http: int, error: str, message: str) -> tuple[int, dict]:
    return http, {"ok": False, "error": error, "messages": [message]}


class SettingsRelayMixin:
    """Settings put relay for ``LiveSource`` (uses ``_servers``, ``_runners``,
    ``_connected``, ``_lock`` and ``_offers_capability``)."""

    def _settings_relay_init(self) -> None:
        self._settings_lock = threading.Lock()
        self._settings_waits: dict[str, _Wait] = {}
        self._settings_reqs = itertools.count(1)

    def _settings_relay_finish(self, req: str | None, data: object, error: str | None) -> bool:
        if req is None:
            return False
        with self._settings_lock:
            wait = self._settings_waits.pop(req, None)
        if wait is None:
            # A late reply to a put that already timed out is still ours:
            # swallow it so it never reaches the deck's result handling.
            return req.startswith(_REQ_PREFIX) and req[len(_REQ_PREFIX) :].isdigit()
        if error is None and isinstance(data, dict) and isinstance(data.get("ok"), bool):
            wait.data = data
        else:
            wait.error = error or "malformed settings reply"
        wait.event.set()
        return True

    def _settings_relay_on_result(self, req: str | None, data: object) -> bool:
        return self._settings_relay_finish(req, data, None)

    def _settings_relay_on_error(self, req: str | None, message: str) -> bool:
        return self._settings_relay_finish(req, None, message or "bridge error")

    def _settings_relay_on_connection(self, server_id: str, up: bool) -> None:
        if up:
            return
        with self._settings_lock:
            victims = [r for r, w in self._settings_waits.items() if w.server_id == server_id]
            waits = [self._settings_waits.pop(r) for r in victims]
        for wait in waits:
            wait.error = "disconnected"
            wait.event.set()

    def bridge_settings_put(
        self,
        server_id: str,
        base_revision: int,
        settings: dict,
        *,
        wait_s: float = SETTINGS_WAIT_S,
    ) -> tuple[int, dict] | None:
        """Relay one put; returns (http status, payload). None = unknown server."""
        if server_id not in self._servers:
            return None
        runner = self._runners.get(server_id)
        with self._lock:
            connected = bool(self._connected.get(server_id))
        if runner is None or not connected:
            return _fail(503, "disconnected", "the server is not connected")
        if not self._offers_capability(server_id, SETTINGS_CAPABILITY):
            return _fail(503, "unsupported", "this bridge does not offer shared settings")
        req = f"{_REQ_PREFIX}{next(self._settings_reqs)}"
        wait = _Wait(server_id)
        with self._settings_lock:
            self._settings_waits[req] = wait
        runner.send(
            {
                "type": "settings_put",
                "req": req,
                "base_revision": base_revision,
                "settings": settings,
            }
        )
        if not wait.event.wait(wait_s):
            with self._settings_lock:
                self._settings_waits.pop(req, None)
            return _fail(504, "timeout", "the bridge did not answer in time")
        if wait.data is None:
            message = wait.error or "failed"
            if message == "disconnected":
                return _fail(503, "disconnected", "the bridge disconnected")
            return _fail(502, "bridge_error", message)
        data = wait.data
        if data["ok"]:
            return 200, {"ok": True, "revision": data.get("revision")}
        error = data.get("error")
        error = error if isinstance(error, str) else "failed"
        messages = data.get("messages")
        payload: dict = {
            "ok": False,
            "error": error,
            "messages": messages if isinstance(messages, list) else [],
        }
        if "revision" in data:
            payload["revision"] = data["revision"]
        return _HTTP.get(error, 502), payload


def handle_post(source, path: str, body: dict) -> tuple[int, dict | None]:
    """POST /bridge-settings/{id} {"base_revision", "settings"}."""
    server_id = route_server_id(path)
    if server_id is None or not callable(getattr(source, "bridge_settings_put", None)):
        return 404, None
    base = body.get("base_revision")
    settings = body.get("settings")
    if isinstance(base, bool) or not isinstance(base, int) or not isinstance(settings, dict):
        return _fail(400, "bad_request", "expected {base_revision: int, settings: object}")
    result = source.bridge_settings_put(server_id, base, settings)
    return result if result is not None else (404, None)
