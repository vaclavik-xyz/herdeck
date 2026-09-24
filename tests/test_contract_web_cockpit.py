"""Contract: the web cockpit as production runs it.

launchd ``dev.herdeck.web`` runs ``python -m herdeck.web run`` with
HERDECK_CONFIG / HERDECK_WEB_{BASE_PATH,BIND,PORT,FRAME_ANCESTORS,PUBLIC_ORIGIN}
behind a reverse proxy; another app ("persos") embeds it in an iframe and
drives the semantic API. These tests start that exact entry point in a
subprocess against a fake bridge and pin every externally visible route,
status code, header and payload shape. They are implementation-agnostic on
purpose: the runtime behind ``herdeck.web`` may change, this contract may not.
"""

from __future__ import annotations

import base64
import http.client
import json
import re
import threading
import time

import pytest
from contract_support import (
    WEB_TOKEN,
    FakeBridge,
    RuntimeProcess,
    base_env,
    pane,
    seed_web_token,
    wait_until,
    write_config,
)

BASE = "/herdeck"
PUBLIC = "https://cockpit.example.test"
CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; "
    f"frame-ancestors {PUBLIC}"
)
SESSION_COOKIE = re.compile(
    r"^herdeck_session=([A-Za-z0-9_-]{20,}); Path=/herdeck/; HttpOnly; "
    r"SameSite=Strict; Secure; Max-Age=28800$"
)


class Client:
    def __init__(self, port: int):
        self.port = port
        self.cookie = ""

    def request(self, method, path, *, body=None, headers=None, timeout=10.0, cookie=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        request_headers = dict(headers or {})
        if cookie and self.cookie:
            request_headers["Cookie"] = f"herdeck_session={self.cookie}"
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            request_headers.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=request_headers)
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        return response.status, response.headers, payload

    def json(self, method, path, **kwargs):
        status, headers, payload = self.request(method, path, **kwargs)
        return status, headers, json.loads(payload) if payload else None

    def login(self):
        status, headers, body = self.json(
            "POST", f"{BASE}/api/v1/browser-sessions", headers={"X-Herdeck-Token": WEB_TOKEN}
        )
        assert status == 201, body
        match = SESSION_COOKIE.match(headers["Set-Cookie"])
        assert match, headers["Set-Cookie"]
        self.cookie = match.group(1)
        return body

    def state(self):
        status, _, body = self.json("GET", f"{BASE}/state")
        assert status == 200
        return body


def inventory(client):
    status, _, body = client.json(
        "GET", f"{BASE}/api/v1/agents", headers={"X-Herdeck-Token": WEB_TOKEN}, cookie=False
    )
    return body["agents"] if status == 200 else []


INITIAL_PANES = [
    pane("p1", "idle", label="alpha"),
    pane("p2", "blocked", label="bravo", agent_type="codex"),
    pane("p3", "idle", label="charlie"),
]


def start_cockpit(tmp_path, panes=None, *, extra_config=""):
    bridge = FakeBridge(panes or INITIAL_PANES)
    home = tmp_path / "home"
    home.mkdir()
    seed_web_token(home)
    config = write_config(home / "config.toml", bridge, extra=extra_config)
    env = base_env(home)
    env.update(
        {
            "HERDECK_CONFIG": str(config),
            "HERDECK_WEB_BASE_PATH": BASE,
            "HERDECK_WEB_BIND": "127.0.0.1",
            "HERDECK_WEB_PORT": "0",
            "HERDECK_WEB_FRAME_ANCESTORS": PUBLIC,
            "HERDECK_WEB_PUBLIC_ORIGIN": PUBLIC,
        }
    )
    proc = RuntimeProcess(["-m", "herdeck.web", "run"], env, cwd=home)
    try:
        match = proc.wait_line(
            r"herdeck web simulator listening on http://127\.0\.0\.1:(\d+)/herdeck/ "
            r"\(authenticated browser session required\)"
        )
        client = Client(int(match.group(1)))
        bridge.wait_connected()
        client.login()
        # the first snapshot arrived: every pane is in the semantic inventory
        expected = len(panes or INITIAL_PANES)
        wait_until(lambda: len(inventory(client)) == expected)
    except BaseException:
        proc.stop()
        bridge.close()
        raise
    return proc, bridge, client, home


@pytest.fixture
def cockpit(tmp_path):
    proc, bridge, client, home = start_cockpit(tmp_path)
    try:
        yield proc, bridge, client
    finally:
        proc.stop()
        bridge.close()


def test_health_routes_base_path_and_not_found(cockpit):
    _proc, _bridge, client = cockpit
    status, headers, body = client.json("GET", f"{BASE}/healthz", cookie=False)
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert headers["Cache-Control"] == "no-store"
    assert body["ok"] is True and body["service"] == "herdeck-web"
    assert set(body) == {"ok", "service", "version", "build"}

    # nothing is served outside the base path, unknown routes are 404
    assert client.request("GET", "/healthz", cookie=False)[0] == 404
    assert client.request("GET", "/state")[0] == 404
    assert client.request("GET", f"{BASE}/nope")[0] == 404
    assert client.request("POST", f"{BASE}/nope")[0] == 404
    assert client.request("DELETE", f"{BASE}/state")[0] == 404

    status, _, ready = client.json("GET", f"{BASE}/readyz")
    assert status == 200
    assert ready["ready"] is True and isinstance(ready["state_version"], int)
    assert client.request("GET", f"{BASE}/readyz", cookie=False)[0] == 403


def test_page_requires_a_session_and_carries_embed_security_headers(cockpit):
    _proc, _bridge, client = cockpit
    for path in (f"{BASE}/", BASE, f"{BASE}/?token={WEB_TOKEN}"):
        status, headers, body = client.request("GET", path, cookie=False)
        assert status == 403, path
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        assert headers["Content-Security-Policy"] == CSP
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert headers["Cache-Control"] == "no-store"
        assert b"Browser session required" in body
        assert "X-Frame-Options" not in headers

    status, headers, page = client.request("GET", f"{BASE}/")
    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert headers["Content-Security-Policy"] == CSP
    assert headers["Referrer-Policy"] == "no-referrer"
    text = page.decode()
    assert "<title>Herdeck simulator</title>" in text
    assert 'src="/herdeck/assets/xterm.js"' in text
    assert 'href="/herdeck/assets/xterm.css"' in text
    assert 'const basePath="/herdeck";' in text
    # the page's JS talks only to these routes (all under the base path)
    for route in ("'/press/'", "'/term-stop/'", "'/term/'", "'/state?since='", "'/tile/'",
                  "'/panel?v='"):
        assert route in text
    assert WEB_TOKEN not in text


def test_browser_session_handoff_and_logout(cockpit):
    _proc, _bridge, client = cockpit
    status, _, body = client.json("POST", f"{BASE}/api/v1/browser-sessions", cookie=False)
    assert status == 401
    assert body == {
        "api_version": "v1",
        "error": {"code": "unauthorized", "message": "missing or invalid credentials"},
    }
    status, _, _ = client.json(
        "POST",
        f"{BASE}/api/v1/browser-sessions",
        headers={"X-Herdeck-Token": "wrong"},
        cookie=False,
    )
    assert status == 401
    # a browser cookie cannot mint more sessions: the header token is required
    status, _, _ = client.json("POST", f"{BASE}/api/v1/browser-sessions")
    assert status == 401

    assert client.login() == {"api_version": "v1", "expires_in": 28800}

    # logout needs the browser session AND the public Origin
    status, _, _ = client.json("DELETE", f"{BASE}/api/v1/browser-sessions/current")
    assert status == 401
    status, headers, body = client.json(
        "DELETE", f"{BASE}/api/v1/browser-sessions/current", headers={"Origin": PUBLIC}
    )
    assert status == 200
    assert body == {"api_version": "v1", "revoked": True}
    assert headers["Set-Cookie"] == (
        "herdeck_session=; Path=/herdeck/; HttpOnly; SameSite=Strict; Secure; Max-Age=0"
    )
    assert client.request("GET", f"{BASE}/state")[0] == 403


def test_state_tiles_panel_assets_and_long_poll(cockpit):
    _proc, bridge, client = cockpit
    state = client.state()
    assert set(state) == {"version", "slots", "cols", "has_panel", "panel", "tiles"}
    assert state["slots"] == 13 and state["cols"] == 5
    assert state["has_panel"] is True
    assert all(0 <= int(index) < 13 for index in state["tiles"])

    status, headers, body = client.request("GET", f"{BASE}/state", cookie=False)
    assert status == 403
    assert headers["Content-Type"] == "text/plain; charset=utf-8"
    assert body.startswith(b"herdeck simulator: a valid browser session is required.")
    # the server-side token authenticates reads without a browser session
    status, _, _ = client.request(
        "GET", f"{BASE}/state", headers={"X-Herdeck-Token": WEB_TOKEN}, cookie=False
    )
    assert status == 200

    index, version = next(iter(state["tiles"].items()))
    status, headers, png = client.request("GET", f"{BASE}/tile/{index}?v={version}")
    assert status == 200 and headers["Content-Type"] == "image/png"
    assert png.startswith(b"\x89PNG")
    assert client.request("GET", f"{BASE}/tile/{index}?v={version + 100000}")[0] == 404
    assert client.request("GET", f"{BASE}/tile/{index}")[0] == 404
    assert client.request("GET", f"{BASE}/tile/{index}?v={version}", cookie=False)[0] == 403
    status, headers, png = client.request("GET", f"{BASE}/panel?v={state['panel']}")
    assert status == 200 and headers["Content-Type"] == "image/png"
    assert png.startswith(b"\x89PNG")

    status, headers, asset = client.request("GET", f"{BASE}/assets/xterm.js", cookie=False)
    assert status == 200
    assert headers["Content-Type"] == "text/javascript; charset=utf-8"
    assert headers["Cache-Control"] == "public, max-age=3600"
    assert asset
    status, headers, _ = client.request("GET", f"{BASE}/assets/xterm.css", cookie=False)
    assert status == 200 and headers["Content-Type"] == "text/css; charset=utf-8"
    assert client.request("GET", f"{BASE}/assets/evil.js", cookie=False)[0] == 404

    # long poll: a request naming the current version is held until a change
    current = client.state()["version"]
    started = time.monotonic()

    result = {}

    def poll():
        result["state"] = client.json("GET", f"{BASE}/state?since={current}", timeout=30)[2]

    poller = threading.Thread(target=poll)
    poller.start()
    time.sleep(0.5)
    assert poller.is_alive(), "a since=<current> request must be held"
    bridge.push_event(pane("p3", "done", label="charlie"))
    poller.join(15)
    assert not poller.is_alive()
    assert result["state"]["version"] > current
    assert time.monotonic() - started < 15


def test_semantic_api_inventory_actions_text_and_decisions(cockpit):
    _proc, bridge, client = cockpit
    token = {"X-Herdeck-Token": WEB_TOKEN}
    status, _, body = client.json("GET", f"{BASE}/api/v1/agents", cookie=False)
    assert status == 401
    assert body["error"]["code"] == "unauthorized"

    def inventory():
        return client.json("GET", f"{BASE}/api/v1/agents", headers=token, cookie=False)[2]

    body = wait_until(
        lambda: (inv := inventory())["agents"] and all(a["available"] for a in inv["agents"])
        and inv
    )
    assert body["api_version"] == "v1"
    agents = {agent["pane_id"]: agent for agent in body["agents"]}
    assert set(agents) == {"p1", "p2", "p3"}
    blocked = agents["p2"]
    assert blocked["status"] == "blocked"
    assert blocked["server_id"] == "local"
    assert blocked["terminal_id"] == "term-p2"
    assert blocked["agent_type"] == "codex"
    assert blocked["label"] == "bravo"
    assert blocked["available"] is True
    assert {"backend", "capabilities", "work", "repository", "branch", "metadata"} <= set(blocked)
    # the browser session reads the inventory too
    assert client.json("GET", f"{BASE}/api/v1/agents")[0] == 200

    target = {"server_id": "local", "pane_id": "p2", "terminal_id": "term-p2"}
    status, _, sent = client.json(
        "POST",
        f"{BASE}/api/v1/actions",
        body={**target, "idempotency_key": "approve-1", "action": "approve"},
        headers=token,
        cookie=False,
    )
    assert status == 200
    assert sent == {"api_version": "v1", "outcome": "sent", "message": "action sent"}
    act = bridge.wait_message(lambda m: m.get("type") == "act")
    assert act["pane_id"] == "p2" and act["terminal_id"] == "term-p2"
    assert act["guard"] is True and act["keys"] == ["y", "enter"]
    # idempotent replay: same answer, nothing new reaches the bridge
    status, _, replay = client.json(
        "POST",
        f"{BASE}/api/v1/actions",
        body={**target, "idempotency_key": "approve-1", "action": "approve"},
        headers=token,
        cookie=False,
    )
    assert (status, replay) == (200, sent)
    time.sleep(0.3)
    assert len(bridge.messages("act")) == 1

    # stop always needs a confirmation round-trip
    status, _, armed = client.json(
        "POST",
        f"{BASE}/api/v1/actions",
        body={**target, "idempotency_key": "stop-1", "action": "stop"},
        headers=token,
        cookie=False,
    )
    assert status == 409 and armed["outcome"] == "confirmation_required"
    assert armed["expires_in"] == 60
    status, _, stopped = client.json(
        "POST",
        f"{BASE}/api/v1/actions",
        body={
            **target,
            "idempotency_key": "stop-2",
            "action": "stop",
            "confirmation": armed["confirmation"],
        },
        headers=token,
        cookie=False,
    )
    assert status == 200 and stopped["outcome"] == "sent"
    bridge.wait_message(lambda m: m.get("type") == "act" and m.get("guard") is False)

    status, _, texted = client.json(
        "POST",
        f"{BASE}/api/v1/text",
        body={**target, "idempotency_key": "text-1", "text": "continue please"},
        headers=token,
        cookie=False,
    )
    assert status == 200 and texted["outcome"] == "sent"
    msg = bridge.wait_message(lambda m: m.get("type") == "send_text")
    assert msg["text"] == "continue please" and msg["pane_id"] == "p2"

    status, _, decisions = client.json(
        "POST", f"{BASE}/api/v1/decisions", body=target, headers=token, cookie=False
    )
    assert status == 200
    assert decisions["outcome"] == "ready"
    assert [c["key"] for c in decisions["choices"]] == ["1", "2", "3"]
    status, _, chosen = client.json(
        "POST",
        f"{BASE}/api/v1/choices",
        body={
            **target,
            "idempotency_key": "choice-1",
            "choice": "1",
            "decision_revision": decisions["decision_revision"],
        },
        headers=token,
        cookie=False,
    )
    assert status == 200 and chosen["outcome"] == "sent"
    choose = bridge.wait_message(lambda m: m.get("type") == "choose_if_blocked")
    assert choose["choice"] == "1"

    # stale identity and validation errors
    status, _, stale = client.json(
        "POST",
        f"{BASE}/api/v1/actions",
        body={**target, "terminal_id": "other", "idempotency_key": "x", "action": "deny"},
        headers=token,
        cookie=False,
    )
    assert status == 409 and stale["outcome"] == "stale_identity"
    status, _, invalid = client.json(
        "POST", f"{BASE}/api/v1/actions", body=b"{nope", headers=token, cookie=False
    )
    assert status == 400 and invalid["error"]["code"] == "invalid_json"
    status, _, invalid = client.json(
        "POST",
        f"{BASE}/api/v1/actions",
        body={**target, "idempotency_key": "y", "action": "explode"},
        headers=token,
        cookie=False,
    )
    assert status == 422 and invalid["error"]["field"] == "action"

    # a browser session writes only from the public origin
    status, _, _ = client.json(
        "POST",
        f"{BASE}/api/v1/actions",
        body={**target, "idempotency_key": "deny-1", "action": "deny"},
    )
    assert status == 401
    status, _, _ = client.json(
        "POST",
        f"{BASE}/api/v1/actions",
        body={**target, "idempotency_key": "deny-1", "action": "deny"},
        headers={"Origin": "https://evil.example"},
    )
    assert status == 401
    status, _, denied = client.json(
        "POST",
        f"{BASE}/api/v1/actions",
        body={**target, "idempotency_key": "deny-1", "action": "deny"},
        headers={"Origin": PUBLIC},
    )
    assert status == 200 and denied["outcome"] == "sent"


def test_press_route_auth_and_bridge_command(cockpit):
    _proc, bridge, client = cockpit
    assert client.request("POST", f"{BASE}/press/0")[0] == 403  # no Origin
    assert client.request(
        "POST", f"{BASE}/press/0", headers={"Origin": "https://evil.example"}
    )[0] == 403
    assert client.request(
        "POST", f"{BASE}/press/0", headers={"Origin": PUBLIC}, cookie=False
    )[0] == 403
    assert client.request("POST", f"{BASE}/press/abc", headers={"Origin": PUBLIC})[0] == 400
    assert client.request("POST", f"{BASE}/press/999", headers={"Origin": PUBLIC})[0] == 204

    before = client.state()["version"]
    seen = len(bridge.messages())
    status, _, _ = client.request("POST", f"{BASE}/press/0", headers={"Origin": PUBLIC})
    assert status == 204
    # the press drilled into an agent: its view re-rendered and the bridge was asked
    wait_until(lambda: client.state()["version"] > before)
    wait_until(
        lambda: any(
            m.get("type") in {"read", "focus"} and m.get("pane_id") in {"p1", "p2", "p3"}
            for m in bridge.messages()[seen:]
        )
    )
    # the server-side token presses without a browser Origin
    assert client.request(
        "POST", f"{BASE}/press/14", headers={"X-Herdeck-Token": WEB_TOKEN}, cookie=False
    )[0] == 204


def _read_sse(response, count, timeout=10.0):
    events = []
    deadline = time.monotonic() + timeout
    while len(events) < count and time.monotonic() < deadline:
        line = response.readline()
        if not line:
            break
        line = line.decode().strip()
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


def _open_terminal(client, *, cols=None, rows=None, timeout=10.0):
    """Open a live preview on the first agent tile (tiles become previewable
    only after a short post-render guard, so retry with fresh stream ids)."""
    deadline = time.monotonic() + timeout
    attempt = 0
    while time.monotonic() < deadline:
        state = client.state()
        for index in ("0", "1", "2"):
            attempt += 1
            stream = f"stream{attempt:04d}x"
            query = f"stream={stream}&v={state['tiles'][index]}"
            if cols is not None:
                query += f"&cols={cols}&rows={rows}"
            conn = http.client.HTTPConnection("127.0.0.1", client.port, timeout=15)
            conn.request(
                "GET",
                f"{BASE}/term/{index}?{query}",
                headers={"Cookie": f"herdeck_session={client.cookie}"},
            )
            response = conn.getresponse()
            if response.status != 200:
                conn.close()
                continue
            first = _read_sse(response, 1)
            if first and first[0]["kind"] == "meta":
                return conn, response, first[0], index, state["tiles"][index], stream
            assert first == [{"kind": "closed", "reason": "no agent terminal on this tile"}]
            conn.close()
        time.sleep(0.2)
    raise AssertionError("no tile became previewable")


def test_live_terminal_sse_and_stop(cockpit):
    _proc, bridge, client = cockpit
    state = client.state()
    index, version = "0", state["tiles"]["0"]
    stream = "streamabc123"

    assert client.request("GET", f"{BASE}/term/{index}?stream={stream}&v={version}",
                          cookie=False)[0] == 403
    assert client.request("GET", f"{BASE}/term/{index}?stream={stream}")[0] == 400  # no v
    assert client.request("GET", f"{BASE}/term/{index}?stream=bad&v={version}")[0] == 400
    assert client.request("GET", f"{BASE}/term/99?stream={stream}&v={version}")[0] == 400
    assert client.request(
        "GET", f"{BASE}/term/{index}?stream={stream}&v={version + 100000}"
    )[0] == 409
    # an empty tile answers with a closed event (not an HTTP error)
    conn = http.client.HTTPConnection("127.0.0.1", client.port, timeout=15)
    conn.request(
        "GET",
        f"{BASE}/term/12?stream=emptytile1&v={state['tiles']['12']}",
        headers={"Cookie": f"herdeck_session={client.cookie}"},
    )
    response = conn.getresponse()
    assert response.status == 200
    assert _read_sse(response, 1) == [
        {"kind": "closed", "reason": "no agent terminal on this tile"}
    ]
    conn.close()

    conn, response, meta, index, version, stream = _open_terminal(client, cols=100, rows=30)
    assert response.headers["Content-Type"] == "text/event-stream; charset=utf-8"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Accel-Buffering"] == "no"
    assert meta["label"] in {"alpha", "bravo", "charlie"}
    events = _read_sse(response, 1)
    assert events[0]["kind"] == "frame"
    assert events[0]["full"] is True
    assert (events[0]["cols"], events[0]["rows"]) == (100, 30)
    assert base64.b64decode(events[0]["data"]) == b"hello from pane"
    observe = bridge.wait_message(lambda m: m.get("type") == "observe")
    assert (observe["cols"], observe["rows"]) == (100, 30)
    assert observe["terminal_id"] == f"term-{observe['pane_id']}"

    # a second viewer of the same stream id is a conflict
    assert client.request("GET", f"{BASE}/term/{index}?stream={stream}&v={version}")[0] == 409

    assert client.request("POST", f"{BASE}/term-stop/{stream}")[0] == 403  # no Origin
    assert client.request("POST", f"{BASE}/term-stop/bad", headers={"Origin": PUBLIC})[0] == 400
    status, _, _ = client.request(
        "POST", f"{BASE}/term-stop/{stream}", headers={"Origin": PUBLIC}
    )
    assert status == 204
    stop = bridge.wait_message(lambda m: m.get("type") == "observe_stop")
    assert stop["req"] == observe["req"]
    assert response.read().strip() == b""  # the stream ends
    conn.close()


def test_live_terminal_defaults_and_bridge_close(cockpit):
    _proc, bridge, client = cockpit
    conn, response, _meta, _index, _version, _stream = _open_terminal(client)
    frame = _read_sse(response, 1)[0]
    assert frame["kind"] == "frame"
    observe = bridge.wait_message(lambda m: m.get("type") == "observe")
    assert (observe["cols"], observe["rows"]) == (80, 24)  # defaults
    bridge.send_all({"type": "term_closed", "req": observe["req"], "reason": "pane exited"})
    assert _read_sse(response, 1) == [{"kind": "closed", "reason": "pane exited"}]
    assert response.read().strip() == b""
    conn.close()


def test_working_agent_tile_animates_without_bridge_traffic(tmp_path):
    proc, bridge, client, _home = start_cockpit(
        tmp_path, [pane("w1", "working", label="busy")]
    )
    try:
        first = client.state()
        seen = len(bridge.messages())
        wait_until(lambda: client.state()["tiles"]["0"] != first["tiles"]["0"], timeout=5)
        assert len(bridge.messages()) == seen
    finally:
        proc.stop()
        bridge.close()


def test_bridge_disconnect_marks_agents_unavailable_and_reconnects(tmp_path):
    proc, bridge, client, _home = start_cockpit(tmp_path)
    try:
        port = bridge.port
        bridge.close()
        wait_until(lambda: not any(a["available"] for a in inventory(client)), timeout=10)
        replacement = FakeBridge(INITIAL_PANES, port=port)
        try:
            replacement.wait_connected(timeout=40)
            wait_until(
                lambda: (agents := inventory(client)) and all(a["available"] for a in agents),
                timeout=10,
            )
        finally:
            replacement.close()
    finally:
        proc.stop()


def test_legacy_query_token_bootstrap_is_opt_in(tmp_path):
    bridge = FakeBridge(INITIAL_PANES)
    home = tmp_path / "home"
    home.mkdir()
    seed_web_token(home)
    config = write_config(home / "config.toml", bridge)
    env = base_env(home)
    env.update({"HERDECK_CONFIG": str(config), "HERDECK_WEB_PORT": "0"})
    proc = RuntimeProcess(
        ["-m", "herdeck.web", "run", "--allow-query-token", "--base-path", "/deck"],
        env,
        cwd=home,
    )
    try:
        match = proc.wait_line(
            r"herdeck web simulator listening on http://127\.0\.0\.1:(\d+)/deck/ "
            r"\(run 'herdeck-web url --allow-query-token' to print the legacy capability URL\)"
        )
        client = Client(int(match.group(1)))
        status, headers, _ = client.request("GET", f"/deck/?token={WEB_TOKEN}")
        assert status == 303
        assert headers["Location"] == "/deck/"
        assert headers["Referrer-Policy"] == "no-referrer"
        cookie = headers["Set-Cookie"]
        assert re.fullmatch(
            r"herdeck_session=[A-Za-z0-9_-]{20,}; Path=/deck/; HttpOnly; "
            r"SameSite=Strict; Max-Age=28800",
            cookie,
        ), cookie
        # the page itself embeds no frame ancestor when none is configured
        client.cookie = cookie.split(";")[0].split("=", 1)[1]
        status, headers, _ = client.request("GET", "/deck/")
        assert status == 200
        assert headers["Content-Security-Policy"].endswith("frame-ancestors 'none'")
        # the legacy query token also authenticates reads directly
        assert client.request("GET", f"/deck/state?token={WEB_TOKEN}", cookie=False)[0] == 200
    finally:
        proc.stop()
        bridge.close()


def test_mock_mode_serves_a_demo_deck_without_a_bridge(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    seed_web_token(home)
    env = base_env(home)
    env.update({"HERDECK_MOCK": "1", "HERDECK_WEB_PORT": "0"})
    proc = RuntimeProcess(["-m", "herdeck.web", "run"], env, cwd=home)
    try:
        match = proc.wait_line(r"listening on http://127\.0\.0\.1:(\d+)/ ")
        client = Client(int(match.group(1)))
        status, _, _ = client.json(
            "POST", "/api/v1/browser-sessions", headers={"X-Herdeck-Token": WEB_TOKEN}
        )
        assert status == 201
        # the demo fleet appears in the semantic inventory once the runtime is up
        wait_until(
            lambda: (
                response := client.json(
                    "GET", "/api/v1/agents", headers={"X-Herdeck-Token": WEB_TOKEN}, cookie=False
                )
            )[0] == 200
            and response[2]["agents"]
        )
        status, _, state = client.json(
            "GET", "/state", headers={"X-Herdeck-Token": WEB_TOKEN}, cookie=False
        )
        assert status == 200 and state["slots"] == 13 and state["has_panel"]
    finally:
        proc.stop()
