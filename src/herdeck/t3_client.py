"""T3 Code orchestration protocol 2 transport: HTTP reads and the WebSocket
dispatch RPC. No retries here (the connector owns delivery); errors never
carry response bodies or credentials.

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

    def __init__(self, reason):
        super().__init__("T3 rejected the command: " + reason)
        self.reason = reason


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

    def _request(self, path, method):
        req = Request(self.url + path, method=method, data=b"" if method == "POST" else None,
                      headers={"Authorization": "Bearer " + self._token,
                               "x-t3-orchestration-protocol": str(PROTOCOL)})
        try:
            with self._opener.open(req, timeout=_TIMEOUT) as response:
                raw = response.read(_LIMIT + 1)
        except HTTPError as exc:
            raise T3Error(f"T3 HTTP {exc.code}", exc.code) from None
        except (URLError, OSError, ValueError, HTTPException):
            raise T3Error("T3 transport or response error") from None
        if len(raw) > _LIMIT:
            raise T3Error("T3 response exceeds limit")
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

    async def dispatch(self, command: dict) -> dict:
        ticket = (await asyncio.to_thread(self.http.post, "/api/auth/websocket-ticket")).get("ticket")
        if not isinstance(ticket, str) or not ticket:
            raise T3Error("Unsupported T3 ticket response")
        url = self._ws_base + "/ws?" + urlencode({"orchestrationProtocol": PROTOCOL, "wsTicket": ticket})
        rid = next(self._ids)
        frame = {"_tag": "Request", "id": rid, "tag": "orchestration.dispatchCommand",
                 "payload": command, "headers": []}
        try:
            async with asyncio.timeout(_TIMEOUT):
                async with websockets.connect(url, proxy=None, max_size=_LIMIT) as ws:
                    await ws.send(json.dumps(frame))
                    async for raw in ws:
                        msg = json.loads(raw)
                        tag = msg.get("_tag")
                        if tag == "Ping":
                            await ws.send(json.dumps({"_tag": "Pong"}))
                        elif tag == "Exit" and msg.get("requestId") == rid:
                            exit_ = msg.get("exit") or {}
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
                                  426: "T3 orchestration protocol mismatch"}[code]) from None
            raise T3Error(f"T3 WebSocket HTTP {code}", code) from None
        except (OSError, TimeoutError, ValueError, websockets.WebSocketException):
            raise T3Error("T3 dispatch outcome unknown") from None
        raise T3Error("T3 dispatch outcome unknown")
