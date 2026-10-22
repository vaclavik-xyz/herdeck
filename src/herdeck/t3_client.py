"""T3 Code orchestration protocol 2 transport: HTTP reads, the WebSocket
dispatch RPC and project favicons (assets.createUrl). No retries here (the
connector owns delivery); errors never carry response bodies or credentials.

Contract: docs/t3-code-integration.md (verified against T3 0.0.46 preview).
"""
from __future__ import annotations

import asyncio
import ipaddress
import itertools
import json
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import websockets

from .project_icon_discovery import ICON_MIMES, MAX_ICON_BYTES, mime_for_path

PROTOCOL = 2
_LIMIT = 8 * 1024 * 1024
_TIMEOUT = 8.0


class T3Error(Exception):
    """A sanitized transport/contract error; never includes response bodies."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class T3Rejected(T3Error):
    """The server answered the command and refused it (it did not run)."""

    def __init__(self, reason, handshake=False):
        super().__init__("T3 rejected the command: " + reason)
        self.reason = reason
        # True when the WebSocket upgrade was refused: the command frame was
        # never read, so on a retry it says nothing about the first attempt.
        self.handshake = handshake


class _TooLarge(T3Error):
    """A response over the caller's byte limit."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class T3Http:
    def __init__(self, url: str, token: str, *, timeout: float = _TIMEOUT):
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
        self._timeout = timeout
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def _raw(self, path, method, limit=_LIMIT):
        """(Content-Type, body) of a response of at most ``limit`` bytes."""
        headers = {"x-t3-orchestration-protocol": str(PROTOCOL)}
        if self._token:  # the descriptor is public; never send an empty bearer
            headers["Authorization"] = "Bearer " + self._token
        req = Request(self.url + path, method=method, data=b"" if method == "POST" else None,
                      headers=headers)
        try:
            with self._opener.open(req, timeout=self._timeout) as response:
                ctype = response.headers.get("Content-Type", "")
                raw = response.read(limit + 1)
        except HTTPError as exc:
            raise T3Error(f"T3 HTTP {exc.code}", exc.code) from None
        except (URLError, OSError, ValueError, HTTPException):
            raise T3Error("T3 transport or response error") from None
        if len(raw) > limit:
            raise _TooLarge("T3 response exceeds limit")
        return ctype, raw

    def _request(self, path, method):
        _, raw = self._raw(path, method)
        try:
            result = json.loads(raw)
        except ValueError:
            raise T3Error("Unsupported T3 response") from None
        if not isinstance(result, dict):
            raise T3Error("Unsupported T3 response")
        return result

    def get(self, path):
        return self._request(path, "GET")

    def post(self, path):
        return self._request(path, "POST")


def local_runtime_origin(base_dir) -> str:
    """HTTP origin of the T3 server running on this machine, from
    ``<base_dir>/userdata/server-runtime.json``. Current T3 writes ``origin``
    (and ``port``); older builds wrote ``host`` + ``port``. Raises OSError /
    ValueError / KeyError when the file is absent or unusable."""
    from pathlib import Path

    state = json.loads((Path(base_dir) / "userdata/server-runtime.json").read_text())
    if not isinstance(state, dict):
        raise ValueError("server-runtime.json is not an object")
    origin = state.get("origin")
    if isinstance(origin, str) and origin:
        url = origin.rstrip("/")
    else:
        host, port = state.get("host") or "127.0.0.1", state["port"]
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        url = f"http://{host}:{port}"
    T3Http(url, "")  # validate before anything is sent to it
    return url


def _rejection(cause) -> str:
    for part in cause if isinstance(cause, list) else []:
        if not isinstance(part, dict):
            continue
        if part.get("_tag") == "Die":
            return "invalid command"
        error = part.get("error") if part.get("_tag") == "Fail" else None
        if isinstance(error, dict):
            if error.get("_tag") == "EnvironmentAuthorizationError":
                return "credential lacks orchestration:operate"
            message = error.get("message")
            if isinstance(message, str) and message:
                return message[:300]
    return "command refused"


class T3Client:
    def __init__(self, url: str, token: str, *, ws_url: str | None = None):
        self.http = T3Http(url, token)
        p = urlsplit(self.http.url)
        self._ws_base = ws_url or ("wss" if p.scheme == "https" else "ws") + "://" + p.netloc
        self._ids = itertools.count(1)

    def descriptor(self) -> dict:
        return self.http.get("/.well-known/t3/environment")

    def shell(self) -> dict:
        shell = self.http.get("/api/orchestration/shell")
        if any(not isinstance(shell.get(k), list) for k in ("threads", "archivedThreads", "projects")):
            raise T3Error("Unsupported T3 shell contract")
        return shell

    def _projection(self, body) -> dict:
        projection = body.get("projection") if isinstance(body, dict) else None
        if not isinstance(projection, dict) or not isinstance(projection.get("thread"), dict):
            raise T3Error("Unsupported T3 thread contract")
        return projection

    def thread_detail(self, thread_id: str) -> dict:
        return self._projection(self.http.get(
            "/api/orchestration/threads/" + quote(thread_id, safe="") + "/bounded"))

    def full_detail(self, thread_id: str) -> dict:
        return self._projection(self.http.get(
            "/api/orchestration/threads/" + quote(thread_id, safe="")))

    async def _call(self, method: str, payload: dict) -> dict | None:
        """The Exit of one WebSocket RPC (None when the stream ends first).
        Transport errors propagate raw; each caller maps them."""
        ticket = (await asyncio.to_thread(self.http.post, "/api/auth/websocket-ticket")).get("ticket")
        if not isinstance(ticket, str) or not ticket:
            raise T3Error("Unsupported T3 ticket response")
        url = self._ws_base + "/ws?" + urlencode({"orchestrationProtocol": PROTOCOL, "wsTicket": ticket})
        rid = next(self._ids)
        frame = {"_tag": "Request", "id": rid, "tag": method, "payload": payload, "headers": []}
        async with asyncio.timeout(_TIMEOUT):
            async with websockets.connect(url, proxy=None, max_size=_LIMIT) as ws:
                await ws.send(json.dumps(frame))
                async for raw in ws:
                    msg = json.loads(raw)
                    tag = msg.get("_tag")
                    if tag == "Ping":
                        await ws.send(json.dumps({"_tag": "Pong"}))
                    elif tag == "Exit" and msg.get("requestId") == rid:
                        return msg.get("exit") or {}
        return None

    async def project_favicon(self, workspace_root: str, favicon_path: str | None = None):
        """(mime, bytes) of a project's favicon as T3 resolves it (the project's
        own faviconPath when set, else T3's discovery), or None when it has none
        or it is not PNG/ICO/SVG within MAX_ICON_BYTES. Needs orchestration:read."""
        resource = {"_tag": "project-favicon", "cwd": workspace_root}
        if favicon_path:
            resource["path"] = favicon_path
        try:
            exit_ = await self._call("assets.createUrl", {"resource": resource})
        except (OSError, TimeoutError, ValueError, websockets.WebSocketException):
            raise T3Error("T3 favicon request failed") from None
        if not exit_ or exit_.get("_tag") != "Success":
            raise T3Error("T3 refused the favicon request")
        value = exit_.get("value") or {}
        rel = value.get("relativeUrl")
        # Signed, short-lived and same-origin; never follow anything else.
        if not isinstance(rel, str) or not rel.startswith("/api/assets/"):
            raise T3Error("Unsupported T3 asset response")
        try:
            ctype, data = await asyncio.to_thread(self.http._raw, rel, "GET", MAX_ICON_BYTES)
        except _TooLarge:
            return None
        except T3Error as exc:
            if exc.code == 404:
                return None  # T3 found no favicon for the project
            raise
        source = value.get("sourcePath")
        mime = mime_for_path(source) if isinstance(source, str) else None
        if mime is None:
            ctype = ctype.split(";")[0].strip().lower()
            mime = ctype if ctype in ICON_MIMES else None
        return (mime, data) if mime and data else None

    async def dispatch(self, command: dict) -> dict:
        try:
            exit_ = await self._call("orchestration.dispatchCommand", command)
            if exit_ is not None:
                if exit_.get("_tag") == "Success":
                    value = exit_.get("value") or {}
                    return {"sequence": value.get("sequence")}
                raise T3Rejected(_rejection(exit_.get("cause")))
        except T3Rejected:
            raise
        except websockets.InvalidStatus as exc:
            code = exc.response.status_code
            if code in (401, 403, 426):
                raise T3Rejected({401: "credential invalid or expired",
                                  403: "credential lacks orchestration:operate",
                                  426: "T3 orchestration protocol mismatch"}[code],
                                 handshake=True) from None
            raise T3Error(f"T3 WebSocket HTTP {code}", code) from None
        except (OSError, TimeoutError, ValueError, websockets.WebSocketException):
            raise T3Error("T3 dispatch outcome unknown") from None
        raise T3Error("T3 dispatch outcome unknown")
