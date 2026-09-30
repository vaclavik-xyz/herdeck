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

from .. import secrets as _secrets
from ..bridge_settings import SETTINGS_CAPABILITY
from ..bridge_telegram import TELEGRAM_CONFIG_CAPABILITY

SETTINGS_WAIT_S = 10.0
_ROUTE_RE = re.compile(r"^/bridge-settings/([^/]+)$")
# Not "st": stats.py owns that prefix (ids hk, ua, st, u, p, r, t are taken).
_REQ_PREFIX = "sp"
_TOKEN_ERRORS = ("invalid", "env_locked", "io_error")
_TELEGRAM_RE = re.compile(r"^/bridge-telegram/([^/]+)(?:/(token|test))?$")
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
        relayed = self._relay(
            server_id,
            SETTINGS_CAPABILITY,
            "this bridge does not offer shared settings",
            {"type": "settings_put", "base_revision": base_revision, "settings": settings},
            wait_s,
        )
        if relayed is None:
            return None
        if isinstance(relayed, tuple):
            return relayed
        return _put_result(relayed)

    def _relay(
        self,
        server_id: str,
        capability: str,
        unsupported: str,
        frame: dict,
        wait_s: float,
    ) -> dict | tuple[int, dict] | None:
        """Send ``frame`` (plus a fresh request id) to the bridge and wait for
        its result. Returns the result dict, a ready ``(http, payload)`` failure
        (503 / 504 / 502) or None for an unknown server. Never logs ``frame``
        (a telegram_token frame carries the secret)."""
        if server_id not in self._servers:
            return None
        runner = self._runners.get(server_id)
        with self._lock:
            connected = bool(self._connected.get(server_id))
        if runner is None or not connected:
            return _fail(503, "disconnected", "the server is not connected")
        if not self._offers_capability(server_id, capability):
            return _fail(503, "unsupported", unsupported)
        req = f"{_REQ_PREFIX}{next(self._settings_reqs)}"
        wait = _Wait(server_id)
        with self._settings_lock:
            self._settings_waits[req] = wait
        runner.send({**frame, "req": req})
        if not wait.event.wait(wait_s):
            with self._settings_lock:
                self._settings_waits.pop(req, None)
            return _fail(504, "timeout", "the bridge did not answer in time")
        if wait.data is None:
            message = wait.error or "failed"
            if message == "disconnected":
                return _fail(503, "disconnected", "the bridge disconnected")
            return _fail(502, "bridge_error", message)
        return wait.data

    def bridge_telegram_put(
        self, server_id: str, base_revision: int, settings: dict, *, wait_s: float = SETTINGS_WAIT_S
    ) -> tuple[int, dict] | None:
        """Relay a telegram_put (same result mapping as a settings put)."""
        relayed = self._relay(
            server_id,
            TELEGRAM_CONFIG_CAPABILITY,
            "this bridge does not offer Telegram config",
            {"type": "telegram_put", "base_revision": base_revision, "settings": settings},
            wait_s,
        )
        if relayed is None or isinstance(relayed, tuple):
            return relayed
        return _put_result(relayed)

    def bridge_telegram_token(
        self,
        server_id: str,
        action: str,
        token: str | None = None,
        *,
        from_local: bool = False,
        wait_s: float = SETTINGS_WAIT_S,
    ) -> tuple[int, dict] | None:
        """Relay telegram_token set/clear. ``from_local`` resolves this
        runtime's own bot token server-side (the [notifications.telegram]
        token_env: env, then keychain); it is never returned to the caller."""
        if server_id not in self._servers:
            return None
        if from_local:
            tg = self._config.notifications.telegram
            token = _secrets.get_secret(tg.token_env) if tg is not None else None
            if not token:
                return _fail(422, "no_local_token", "this runtime has no Telegram token")
        frame: dict = {"type": "telegram_token", "action": action}
        if action == "set":
            frame["token"] = token
        relayed = self._relay(
            server_id,
            TELEGRAM_CONFIG_CAPABILITY,
            "this bridge does not offer Telegram config",
            frame,
            wait_s,
        )
        if relayed is None or isinstance(relayed, tuple):
            if isinstance(relayed, tuple) and relayed[0] == 502:
                # An error frame: never forward its text on the token path.
                return _fail(502, "bridge_error", "the bridge refused the token request")
            return relayed
        if relayed["ok"]:
            return 200, {"ok": True}
        error = relayed.get("error")
        error = error if error in _TOKEN_ERRORS else "failed"
        return (422 if error in _TOKEN_ERRORS else 502), {"ok": False, "error": error}

    def bridge_telegram_test(
        self, server_id: str, *, wait_s: float = SETTINGS_WAIT_S
    ) -> tuple[int, dict] | None:
        relayed = self._relay(
            server_id,
            TELEGRAM_CONFIG_CAPABILITY,
            "this bridge does not offer Telegram config",
            {"type": "telegram_test"},
            wait_s,
        )
        if relayed is None or isinstance(relayed, tuple):
            return relayed
        payload: dict = {"ok": relayed["ok"]}
        if isinstance(relayed.get("error"), str):
            payload["error"] = relayed["error"]
        return 200, payload


def _put_result(data: dict) -> tuple[int, dict]:
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


def telegram_route(path: str) -> tuple[str, str] | None:
    """``/bridge-telegram/<id>[/token|/test]`` -> (server id, "" | "token" | "test")."""
    match = _TELEGRAM_RE.fullmatch(path)
    if match is None:
        return None
    server_id = unquote(match.group(1))
    return (server_id, match.group(2) or "") if server_id else None


def handle_telegram_post(source, path: str, body: dict) -> tuple[int, dict | None]:
    """POST /bridge-telegram/{id}[/token|/test]. Bodies are never echoed."""
    route = telegram_route(path)
    if route is None:
        return 404, None
    server_id, sub = route
    if sub == "":
        if not callable(getattr(source, "bridge_telegram_put", None)):
            return 404, None
        base = body.get("base_revision")
        settings = body.get("settings")
        if isinstance(base, bool) or not isinstance(base, int) or not isinstance(settings, dict):
            return _fail(400, "bad_request", "expected {base_revision: int, settings: object}")
        result = source.bridge_telegram_put(server_id, base, settings)
    elif sub == "token":
        if not callable(getattr(source, "bridge_telegram_token", None)):
            return 404, None
        action = body.get("action")
        token = body.get("token")
        from_local = body.get("from_local")
        if action not in ("set", "clear") or (from_local is not None and from_local is not True):
            return _fail(400, "bad_request", "expected {action: set|clear, token?}")
        if action == "set":
            if (from_local is True) == (token is not None) or (
                token is not None and (not isinstance(token, str) or not token)
            ):
                return _fail(400, "bad_request", "set needs exactly one of token, from_local")
        elif token is not None or from_local is not None:
            return _fail(400, "bad_request", "clear takes no token")
        result = source.bridge_telegram_token(
            server_id, action, token if action == "set" else None, from_local=from_local is True
        )
    else:
        if not callable(getattr(source, "bridge_telegram_test", None)):
            return 404, None
        result = source.bridge_telegram_test(server_id)
    return result if result is not None else (404, None)
