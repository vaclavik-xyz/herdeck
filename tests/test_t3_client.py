import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import websockets

from herdeck.t3_client import T3Client, T3Error, T3Http, T3Rejected


class _Handler(BaseHTTPRequestHandler):
    routes = {}
    seen = []

    def log_message(self, *a):
        pass

    def _answer(self):
        _Handler.seen.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}))
        status, body, *ctype = _Handler.routes.get((self.command, self.path), (404, {"code": "not_found"}))
        raw = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", ctype[0] if ctype else "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = _answer
    do_POST = _answer


@pytest.fixture
def http_server():
    _Handler.routes, _Handler.seen = {}, []
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


SHELL = {"schemaVersion": 1, "snapshotSequence": 3, "threads": [], "archivedThreads": [], "projects": []}


def test_reads_send_bearer_and_protocol_header(http_server):
    _, url = http_server
    _Handler.routes[("GET", "/api/orchestration/shell")] = (200, SHELL)
    assert T3Client(url, "tok").shell()["snapshotSequence"] == 3
    _, _, headers = _Handler.seen[-1]
    assert headers["authorization"] == "Bearer tok"
    assert headers["x-t3-orchestration-protocol"] == "2"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500])
def test_http_errors_carry_the_code_never_the_body(http_server, status):
    _, url = http_server
    _Handler.routes[("GET", "/api/orchestration/shell")] = (status, {"secret": "leak"})
    with pytest.raises(T3Error) as err:
        T3Client(url, "tok").shell()
    assert err.value.code == status
    assert "leak" not in str(err.value)


def test_shell_contract_is_validated(http_server):
    _, url = http_server
    _Handler.routes[("GET", "/api/orchestration/shell")] = (200, {"threads": []})
    with pytest.raises(T3Error, match="Unsupported T3 shell contract"):
        T3Client(url, "tok").shell()


@pytest.mark.parametrize("url", ["http://example.com", "http://127.0.0.1/?token=x", "http://u:p@127.0.0.1", "http://127.0.0.1/path"])
def test_unsafe_origins_are_rejected(url):
    with pytest.raises(ValueError):
        T3Http(url, "tok")


async def _ws_server(reply):
    """A fake T3 /ws endpoint; `reply(frame, ws)` answers one request."""
    seen = {}

    async def handler(ws):
        seen["path"] = ws.request.path
        await ws.send(json.dumps({"_tag": "Ping"}))
        async for raw in ws:
            frame = json.loads(raw)
            if frame.get("_tag") == "Pong":
                seen["pong"] = True
                continue
            seen["frame"] = frame
            await reply(frame, ws)

    server = await websockets.serve(handler, "127.0.0.1", 0)
    return server, seen


def _client(port, http_url):
    return T3Client(http_url, "tok", ws_url=f"ws://127.0.0.1:{port}")


def test_dispatch_success_uses_ticket_protocol_and_request_frame(http_server):
    _, url = http_server
    _Handler.routes[("POST", "/api/auth/websocket-ticket")] = (200, {"ticket": "tk", "expiresAt": "x"})

    async def run():
        async def reply(frame, ws):
            await ws.send(json.dumps({"_tag": "Exit", "requestId": frame["id"],
                                      "exit": {"_tag": "Success", "value": {"sequence": 9}}}))
        server, seen = await _ws_server(reply)
        port = server.sockets[0].getsockname()[1]
        try:
            result = await _client(port, url).dispatch({"type": "thread.visit", "commandId": "c1", "threadId": "t", "visitedAt": "2026-10-14T00:00:00.000Z"})
        finally:
            server.close()
        return result, seen

    result, seen = asyncio.run(run())
    assert result == {"sequence": 9}
    assert "orchestrationProtocol=2" in seen["path"] and "wsTicket=tk" in seen["path"]
    assert seen["frame"]["_tag"] == "Request" and seen["frame"]["tag"] == "orchestration.dispatchCommand"
    assert seen["frame"]["headers"] == [] and seen["frame"]["payload"]["commandId"] == "c1"
    assert seen.get("pong") is True


@pytest.mark.parametrize("cause,reason", [
    ([{"_tag": "Fail", "error": {"_tag": "OrchestrationV2DispatchCommandError", "message": "No orchestration projection exists for thread nope."}}],
     "No orchestration projection exists for thread nope."),
    ([{"_tag": "Die", "defect": "Expected ... long schema text"}], "invalid command"),
    ([{"_tag": "Fail", "error": {"_tag": "EnvironmentAuthorizationError", "message": "x", "requiredScope": "orchestration:operate"}}],
     "credential lacks orchestration:operate"),
])
def test_dispatch_failures_are_rejections(http_server, cause, reason):
    _, url = http_server
    _Handler.routes[("POST", "/api/auth/websocket-ticket")] = (200, {"ticket": "tk", "expiresAt": "x"})

    async def run():
        async def reply(frame, ws):
            await ws.send(json.dumps({"_tag": "Exit", "requestId": frame["id"], "exit": {"_tag": "Failure", "cause": cause}}))
        server, _ = await _ws_server(reply)
        port = server.sockets[0].getsockname()[1]
        try:
            await _client(port, url).dispatch({"type": "thread.visit", "commandId": "c", "threadId": "t", "visitedAt": "x"})
        finally:
            server.close()

    with pytest.raises(T3Rejected) as err:
        asyncio.run(run())
    assert err.value.reason == reason


def test_dropped_socket_is_an_unknown_outcome_not_a_rejection(http_server):
    _, url = http_server
    _Handler.routes[("POST", "/api/auth/websocket-ticket")] = (200, {"ticket": "tk", "expiresAt": "x"})

    async def run():
        async def reply(frame, ws):
            await ws.close()
        server, _ = await _ws_server(reply)
        port = server.sockets[0].getsockname()[1]
        try:
            await _client(port, url).dispatch({"type": "thread.visit", "commandId": "c", "threadId": "t", "visitedAt": "x"})
        finally:
            server.close()

    with pytest.raises(T3Error) as err:
        asyncio.run(run())
    assert not isinstance(err.value, T3Rejected)


@pytest.mark.parametrize("status,reason", [
    (401, "credential invalid or expired"),
    (403, "credential lacks orchestration:operate"),
    (426, "T3 orchestration protocol mismatch"),
])
def test_refused_upgrade_is_a_handshake_rejection(http_server, status, reason):
    _, url = http_server
    _Handler.routes[("POST", "/api/auth/websocket-ticket")] = (200, {"ticket": "tk", "expiresAt": "x"})

    async def run():
        def refuse(connection, request):
            return connection.respond(status, "no\n")

        async def handler(ws):
            pass
        server = await websockets.serve(handler, "127.0.0.1", 0, process_request=refuse)
        port = server.sockets[0].getsockname()[1]
        try:
            await _client(port, url).dispatch({"type": "thread.visit", "commandId": "c", "threadId": "t", "visitedAt": "x"})
        finally:
            server.close()

    with pytest.raises(T3Rejected) as err:
        asyncio.run(run())
    assert err.value.reason == reason and err.value.handshake is True


def test_a_command_rejection_is_not_a_handshake_rejection():
    assert T3Rejected("x").handshake is False


def test_other_upgrade_statuses_are_unknown_outcomes_with_the_code(http_server):
    _, url = http_server
    _Handler.routes[("POST", "/api/auth/websocket-ticket")] = (200, {"ticket": "tk", "expiresAt": "x"})

    async def run():
        async def handler(ws):
            pass
        server = await websockets.serve(handler, "127.0.0.1", 0,
                                        process_request=lambda c, r: c.respond(503, "no\n"))
        port = server.sockets[0].getsockname()[1]
        try:
            await _client(port, url).dispatch({"type": "thread.visit", "commandId": "c", "threadId": "t", "visitedAt": "x"})
        finally:
            server.close()

    with pytest.raises(T3Error) as err:
        asyncio.run(run())
    assert not isinstance(err.value, T3Rejected) and err.value.code == 503


# --- project favicons (assets.createUrl) ----------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 32


def _favicon(http_server, value, *, exit_tag="Success", asset=(200, PNG), ctype="image/png"):
    """Run project_favicon against a fake /ws answering ``value`` and a fake asset route."""
    _, url = http_server
    _Handler.routes[("POST", "/api/auth/websocket-ticket")] = (200, {"ticket": "tk", "expiresAt": "x"})
    _Handler.routes[("GET", "/api/assets/abc")] = (*asset, ctype)

    async def run():
        async def reply(frame, ws):
            exit_ = ({"_tag": "Success", "value": value} if exit_tag == "Success" else
                     {"_tag": "Failure", "cause": [{"_tag": "Fail", "error": {"_tag": "AssetAccessError"}}]})
            await ws.send(json.dumps({"_tag": "Exit", "requestId": frame["id"], "exit": exit_}))
        server, seen = await _ws_server(reply)
        port = server.sockets[0].getsockname()[1]
        try:
            return await _client(port, url).project_favicon("/Users/dev/proj", "app/icon.png"), seen
        finally:
            server.close()

    return asyncio.run(run())


def test_project_favicon_asks_t3_for_a_signed_url_and_reads_it(http_server):
    result, seen = _favicon(http_server, {"relativeUrl": "/api/assets/abc", "sourcePath": "/Users/dev/proj/app/icon.png"})
    assert result == ("image/png", PNG)
    assert seen["frame"]["tag"] == "assets.createUrl"
    assert seen["frame"]["payload"] == {"resource": {"_tag": "project-favicon", "cwd": "/Users/dev/proj",
                                                     "path": "app/icon.png"}}


def test_project_favicon_takes_the_mime_from_the_content_type_without_a_source(http_server):
    result, _ = _favicon(http_server, {"relativeUrl": "/api/assets/abc"}, ctype="image/svg+xml; charset=utf-8")
    assert result == ("image/svg+xml", PNG)


@pytest.mark.parametrize("value,asset,ctype", [
    ({"relativeUrl": "/api/assets/abc"}, (404, {"code": "not_found"}), "application/json"),  # no favicon
    ({"relativeUrl": "/api/assets/abc", "sourcePath": "/p/icon.icns"}, (200, PNG), "application/octet-stream"),
    ({"relativeUrl": "/api/assets/abc"}, (200, b"x" * (256 * 1024 + 1)), "image/png"),  # over MAX_ICON_BYTES
])
def test_project_favicon_is_none_when_missing_unsupported_or_too_large(http_server, value, asset, ctype):
    result, _ = _favicon(http_server, value, asset=asset, ctype=ctype)
    assert result is None


@pytest.mark.parametrize("value,exit_tag", [
    ({"relativeUrl": "http://evil.example/api/assets/abc"}, "Success"),
    ({"relativeUrl": "/api/orchestration/shell"}, "Success"),
    ({}, "Failure"),
])
def test_project_favicon_refuses_foreign_urls_and_rejections(http_server, value, exit_tag):
    with pytest.raises(T3Error):
        _favicon(http_server, value, exit_tag=exit_tag)
