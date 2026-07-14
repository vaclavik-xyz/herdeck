import asyncio
import concurrent.futures
import http.cookiejar
import io
import json
import urllib.error
import urllib.request

import pytest
from PIL import Image

from herdeck.app_control import RuntimeAgentControl
from herdeck.config import AnswerProfile, Config
from herdeck.driver.web import WebDeck
from herdeck.model import AgentKey, AgentState, Status, WorkContext
from herdeck.semantic_api import SemanticAPI


class StubIcons:
    def render_tile_bytes(self, tile):
        output = io.BytesIO()
        Image.new("RGB", (10, 10), (1, 2, 3)).save(output, "PNG")
        return output.getvalue()


class CockpitHarness:
    def __init__(self, tmp_path, *, session_clock=None):
        self.agent = AgentState(
            AgentKey("local", "pane-1"),
            "codex",
            "persOS task",
            Status.BLOCKED,
            repo="herdeck",
            branch="feat/api",
            terminal_id="terminal-1",
            work=WorkContext("github", "#33", "e2e", "https://example.test/33"),
        )
        self.available = True
        self.generation = 1
        self.sent = []
        config = Config(
            servers=[],
            profiles={
                "default": AnswerProfile(["y"], ["n"], ["ctrl+c"], ["a"]),
                "codex": AnswerProfile(["y", "enter"], ["n", "enter"], ["ctrl+c"], ["a"]),
            },
            overview_order=["local"],
            grid=(5, 3),
        )
        self.control = RuntimeAgentControl(
            config,
            send=self._send,
            current_agent=self._current_agent,
        )
        self.api = SemanticAPI(
            self.control,
            agents=lambda: [self.agent],
            server_available=lambda _server: self.available,
            generation=lambda _server, _pane: self.generation,
        )
        self.deck = WebDeck(
            slots=4,
            host="127.0.0.1",
            port=0,
            serve=True,
            icon_provider=StubIcons(),
            press_token="persistent-secret-token",
            base_path="/herdeck",
            session_ttl=30,
            session_clock=session_clock,
        )
        self.deck.on_semantic(self._dispatch)
        self.origin = f"http://{self.deck.host}:{self.deck.port}"
        self.base = f"{self.origin}/herdeck"
        jar = http.cookiejar.CookieJar()
        self.browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def close(self):
        self.deck.close()

    def _current_agent(self, key):
        return self.agent if self.agent.key == key else None

    async def _send(self, command, request_id):
        self.sent.append(command)
        result = (
            {"text": "Choose the next step:\n1. Continue\n2) Explain first"}
            if command.kind == "read"
            else {"sent": True}
        )
        asyncio.get_running_loop().call_soon(
            lambda: self.control.handle_result(
                request_id,
                result,
                server_id=command.server_id,
            )
        )

    def _dispatch(self, request):
        future = concurrent.futures.Future()
        try:
            future.set_result(asyncio.run(self.api.handle(request)))
        except Exception as exc:
            future.set_exception(exc)
        return future

    def handoff(self):
        request = urllib.request.Request(
            f"{self.base}/api/v1/browser-sessions",
            method="POST",
            headers={"X-Herdeck-Token": self.deck.press_token},
        )
        with self.browser.open(request, timeout=2) as response:
            return response, json.load(response)

    def json_request(self, path, *, method="GET", body=None, browser=True, headers=None):
        request_headers = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers=request_headers,
        )
        opener = self.browser if browser else urllib.request
        with opener.open(request, timeout=3) as response:
            return response.status, json.load(response), response.headers


def action_payload(action, request_id, **extra):
    return {
        "server_id": "local",
        "pane_id": "pane-1",
        "terminal_id": "terminal-1",
        "idempotency_key": request_id,
        "action": action,
        **extra,
    }


def test_persos_cockpit_full_contract_through_prefixed_live_http(tmp_path):
    harness = CockpitHarness(tmp_path)
    try:
        response, handoff = harness.handoff()
        cookie = response.headers["Set-Cookie"]
        assert handoff == {"api_version": "v1", "expires_in": 30}
        assert "herdeck_session=" in cookie
        assert "Path=/herdeck/" in cookie
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
        assert harness.deck.press_token not in cookie + json.dumps(handoff)

        with harness.browser.open(f"{harness.base}/", timeout=2) as page:
            assert page.status == 200
            assert "token=" not in page.geturl()
            assert b"Herdeck simulator" in page.read()

        status, inventory, _ = harness.json_request("/api/v1/agents")
        assert status == 200
        assert inventory["agents"][0]["terminal_id"] == "terminal-1"
        assert inventory["agents"][0]["work"]["item"] == "#33"

        write_headers = {"Origin": harness.origin}
        status, approved, _ = harness.json_request(
            "/api/v1/actions",
            method="POST",
            body=action_payload("approve", "approve-1"),
            headers=write_headers,
        )
        assert status == 200 and approved["outcome"] == "sent"

        _, denied, _ = harness.json_request(
            "/api/v1/actions",
            method="POST",
            body=action_payload("deny", "deny-1"),
            headers=write_headers,
        )
        assert denied["outcome"] == "sent"

        with pytest.raises(urllib.error.HTTPError) as armed_error:
            harness.json_request(
                "/api/v1/actions",
                method="POST",
                body=action_payload("stop", "stop-1"),
                headers=write_headers,
            )
        assert armed_error.value.code == 409
        armed = json.load(armed_error.value)
        assert armed["outcome"] == "confirmation_required"
        _, stopped, _ = harness.json_request(
            "/api/v1/actions",
            method="POST",
            body=action_payload("stop", "stop-2", confirmation=armed["confirmation"]),
            headers=write_headers,
        )
        assert stopped["outcome"] == "sent"

        text_body = {
            "server_id": "local",
            "pane_id": "pane-1",
            "terminal_id": "terminal-1",
            "idempotency_key": "text-1",
            "text": "continue safely",
        }
        _, sent_text, _ = harness.json_request(
            "/api/v1/text",
            method="POST",
            body=text_body,
            headers=write_headers,
        )
        _, replayed_text, _ = harness.json_request(
            "/api/v1/text",
            method="POST",
            body=text_body,
            headers=write_headers,
        )
        assert sent_text == replayed_text

        _, decisions, _ = harness.json_request(
            "/api/v1/decisions",
            method="POST",
            body={
                "server_id": "local",
                "pane_id": "pane-1",
                "terminal_id": "terminal-1",
            },
            headers=write_headers,
        )
        assert decisions == {
            "api_version": "v1",
            "outcome": "ready",
            "decision_revision": decisions["decision_revision"],
            "choices": [
                {"key": "1", "label": "Continue"},
                {"key": "2", "label": "Explain first"},
            ],
        }

        choice_body = {
            "server_id": "local",
            "pane_id": "pane-1",
            "terminal_id": "terminal-1",
            "idempotency_key": "choice-1",
            "choice": "2",
            "decision_revision": decisions["decision_revision"],
        }
        _, chosen, _ = harness.json_request(
            "/api/v1/choices",
            method="POST",
            body=choice_body,
            headers=write_headers,
        )
        assert chosen["outcome"] == "sent"
        assert [command.kind for command in harness.sent] == [
            "act_if_blocked",
            "act_if_blocked",
            "act_force",
            "send_text",
            "read",
            "choose_if_blocked",
        ]

        with pytest.raises(urllib.error.HTTPError) as outside:
            harness.browser.open(f"{harness.origin}/api/v1/agents", timeout=2)
        assert outside.value.code == 404
    finally:
        harness.close()


def test_cockpit_rejects_bad_origin_stale_identity_offline_and_bad_credentials(tmp_path):
    harness = CockpitHarness(tmp_path)
    try:
        harness.handoff()
        with pytest.raises(urllib.error.HTTPError) as origin_error:
            harness.json_request(
                "/api/v1/actions",
                method="POST",
                body=action_payload("approve", "approve-1"),
                headers={"Origin": "https://attacker.example"},
            )
        assert origin_error.value.code == 401
        assert json.load(origin_error.value)["error"]["code"] == "unauthorized"

        harness.agent = AgentState(
            AgentKey("local", "pane-1"), "codex", "new", Status.BLOCKED, terminal_id="terminal-2"
        )
        with pytest.raises(urllib.error.HTTPError) as stale_error:
            harness.json_request(
                "/api/v1/actions",
                method="POST",
                body=action_payload("approve", "approve-2"),
                headers={"Origin": harness.origin},
            )
        assert stale_error.value.code == 409
        assert json.load(stale_error.value)["outcome"] == "stale_identity"

        harness.agent.terminal_id = "terminal-1"
        harness.available = False
        with pytest.raises(urllib.error.HTTPError) as offline_error:
            harness.json_request(
                "/api/v1/text",
                method="POST",
                body={
                    "server_id": "local",
                    "pane_id": "pane-1",
                    "terminal_id": "terminal-1",
                    "idempotency_key": "text-1",
                    "text": "hello",
                },
                headers={"Origin": harness.origin},
            )
        assert offline_error.value.code == 503
        assert json.load(offline_error.value)["outcome"] == "unavailable_target"

        request = urllib.request.Request(
            f"{harness.base}/api/v1/agents",
            headers={"X-Herdeck-Token": "wrong-secret"},
        )
        with pytest.raises(urllib.error.HTTPError) as auth_error:
            urllib.request.urlopen(request, timeout=2)
        body = json.load(auth_error.value)
        assert auth_error.value.code == 401
        assert body["error"]["code"] == "unauthorized"
        assert "persistent-secret-token" not in json.dumps(body)
        assert harness.sent == []
    finally:
        harness.close()


def test_browser_session_expiry_and_logout_revoke_access(tmp_path):
    clock = [100.0]
    harness = CockpitHarness(tmp_path, session_clock=lambda: clock[0])
    try:
        harness.handoff()
        assert harness.json_request("/api/v1/agents")[0] == 200

        request = urllib.request.Request(
            f"{harness.base}/api/v1/browser-sessions/current",
            method="DELETE",
            headers={"Origin": harness.origin},
        )
        with harness.browser.open(request, timeout=2) as response:
            body = json.load(response)
            assert body["revoked"] is True
            assert "Max-Age=0" in response.headers["Set-Cookie"]
        with pytest.raises(urllib.error.HTTPError) as revoked:
            harness.json_request("/api/v1/agents")
        assert revoked.value.code == 401

        harness.handoff()
        clock[0] += 31
        with pytest.raises(urllib.error.HTTPError) as expired:
            harness.json_request("/api/v1/agents")
        assert expired.value.code == 401
    finally:
        harness.close()


def test_server_header_controls_need_no_browser_origin_and_cross_origin_cookie_is_secure(
    tmp_path,
):
    harness = CockpitHarness(tmp_path)
    try:
        body = json.dumps(action_payload("approve", "server-approve")).encode()
        request = urllib.request.Request(
            f"{harness.base}/api/v1/actions",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Herdeck-Token": harness.deck.press_token,
            },
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            assert json.load(response)["outcome"] == "sent"
    finally:
        harness.close()

    deck = WebDeck(
        slots=4,
        host="127.0.0.1",
        port=0,
        serve=True,
        icon_provider=StubIcons(),
        press_token="persistent-secret-token",
        base_path="/herdeck",
        public_origin="https://cockpit.example",
        frame_ancestors=("https://persos.example",),
    )
    try:
        request = urllib.request.Request(
            f"http://{deck.host}:{deck.port}/herdeck/api/v1/browser-sessions",
            method="POST",
            headers={"X-Herdeck-Token": deck.press_token},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            cookie = response.headers["Set-Cookie"]
        assert "Path=/herdeck/" in cookie
        assert "SameSite=None" in cookie
        assert "Secure" in cookie
    finally:
        deck.close()


def test_malformed_utf8_text_request_fails_without_backend_delivery(tmp_path):
    harness = CockpitHarness(tmp_path)
    try:
        harness.handoff()
        request = urllib.request.Request(
            f"{harness.base}/api/v1/text",
            data=b'{"text":"\xff"}',
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": harness.origin,
            },
        )
        with pytest.raises(urllib.error.HTTPError) as malformed:
            harness.browser.open(request, timeout=2)
        assert malformed.value.code == 400
        assert json.load(malformed.value)["error"]["code"] == "invalid_json"
        assert harness.sent == []
    finally:
        harness.close()


def test_http_semantic_timeout_cancels_queued_runtime_request(tmp_path):
    deck = WebDeck(
        slots=4,
        host="127.0.0.1",
        port=0,
        serve=True,
        icon_provider=StubIcons(),
        press_token="persistent-secret-token",
    )
    deck.SEMANTIC_TIMEOUT = 0.01
    pending = concurrent.futures.Future()
    deck.on_semantic(lambda _request: pending)
    try:
        request = urllib.request.Request(
            f"http://{deck.host}:{deck.port}/api/v1/actions",
            data=json.dumps(action_payload("approve", "timeout-1")).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Herdeck-Token": deck.press_token,
            },
        )
        with pytest.raises(urllib.error.HTTPError) as timed_out:
            urllib.request.urlopen(request, timeout=2)
        assert timed_out.value.code == 504
        assert json.load(timed_out.value)["error"]["code"] == "timeout"
        assert pending.cancelled()
    finally:
        deck.close()
