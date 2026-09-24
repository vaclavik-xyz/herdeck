"""Desktop agent card: the runtime's /agent/* routes and the LiveSource card API.

The card shows ONE agent in full (the deck drill caps the prompt at three panel
lines): header, the whole blocked prompt, the parsed options, a free-text reply,
Stop/Focus. Everything is driven through a FAKE runner (no bridge, no asyncio);
a runner can answer synchronously, the way the connector's on_result would.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest
from test_deckapp_live import StubIcons

from herdeck.config import DEFAULT_PROFILES, Config, SafetyConfig, ServerConfig
from herdeck.decisions import decision_revision
from herdeck.deckapp import DeckApp, MockSource
from herdeck.deckapp.agent_card import sanitize_prompt
from herdeck.deckapp.live import LiveSource
from herdeck.model import AgentKey, AgentState, Status
from herdeck.orchestrator import Orchestrator
from herdeck.protocol import Error, decode_inbound

PROMPT = "Bash command\n  rm -rf build\nDo you want to proceed?\n❯ 1. Yes\n  2. Yes, and don't ask again\n  3. No"


class ReplyingRunner:
    """Captures sends; ``reply(msg)`` may return a result dict (delivered via the
    source's _on_result), an ``Error`` (via _on_request_error) or None (silence)."""

    def __init__(self, reply=None):
        self.sent: list[dict] = []
        self.reply = reply
        self.src = None

    def send(self, msg):
        self.sent.append(msg)
        if self.reply is None or msg.get("type") == "list":
            return
        out = self.reply(msg)
        if isinstance(out, Error):
            self.src._on_request_error("prod", out.req, out.message)
        elif out is not None:
            self.src._on_result("prod", msg.get("req"), out)

    def close(self):
        pass


def make(reply=None, *, confirm=("act_force",), approve_always=True):
    server = ServerConfig(id="prod", url="ws://bridge.local:8765", token="t")
    config = Config(
        servers=[server],
        profiles=dict(DEFAULT_PROFILES),
        overview_order=["prod"],
        grid=(5, 3),
        safety=SafetyConfig(approve_always=approve_always, require_confirm_for=list(confirm)),
    )
    src = LiveSource(config, server)
    runner = ReplyingRunner(reply)
    runner.src = src
    src.attach_runner(runner, "prod")
    clock = [1000.0]
    app = DeckApp(src, serve=False, icon_provider=StubIcons(), clock=lambda: clock[0])
    src._on_connection("prod", True)
    return app, src, runner, clock


def blocked(pane="p0", terminal_id="term-1", **kw):
    return AgentState(
        AgentKey("prod", pane),
        "claude",
        pane,
        Status.BLOCKED,
        repo="herdeck",
        branch="main",
        workspace="ws",
        tab="tab",
        terminal_id=terminal_id,
        **kw,
    )


def cache_prompt(src, runner, pane="p0", text=PROMPT):
    """Answer the background pre-read the blocked snapshot issued."""
    read = [m for m in runner.sent if m["type"] == "read" and m["pane_id"] == pane][-1]
    src._on_result("prod", read["req"], {"text": text, "pane_id": pane})


def blocked_with_prompt(reply=None, **kw):
    app, src, runner, clock = make(reply, **kw)
    src._on_snapshot("prod", [blocked()])
    cache_prompt(src, runner)
    runner.sent.clear()
    return app, src, runner, clock


# --- orchestrator: the drill's option parsing, as data -----------------------


def test_answer_options_match_the_drill_parsing():
    server = ServerConfig(id="prod", url="ws://x", token="t")
    config = Config(servers=[server], profiles=dict(DEFAULT_PROFILES), overview_order=["prod"], grid=(5, 3))
    orch = Orchestrator(config, slots=13)
    orch.apply_snapshot("prod", [blocked()])
    options = orch.answer_options(AgentKey("prod", "p0"), PROMPT)
    assert [(o["key"], o["id"], o["kind"]) for o in options] == [
        ("1", "approve", "option"),
        ("2", "approve_always", "option"),
        ("3", "deny", "option"),
    ]
    assert options[0]["label"] == "Yes"


def test_answer_options_fall_back_to_profile_actions_for_a_yes_no_prompt():
    server = ServerConfig(id="prod", url="ws://x", token="t")
    config = Config(servers=[server], profiles=dict(DEFAULT_PROFILES), overview_order=["prod"], grid=(5, 3))
    orch = Orchestrator(config, slots=13)
    orch.apply_snapshot("prod", [blocked()])
    options = orch.answer_options(AgentKey("prod", "p0"), "Continue? (y/n)")
    assert [o["id"] for o in options] == ["approve", "approve_always", "deny"]
    assert all(o["kind"] == "fallback" for o in options)
    # No blind approval before the prompt was read (same rule as the drill).
    assert orch.answer_options(AgentKey("prod", "p0"), "") == []


def test_answer_options_honour_the_approve_always_safety_switch():
    server = ServerConfig(id="prod", url="ws://x", token="t")
    config = Config(
        servers=[server],
        profiles=dict(DEFAULT_PROFILES),
        overview_order=["prod"],
        grid=(5, 3),
        safety=SafetyConfig(approve_always=False),
    )
    orch = Orchestrator(config, slots=13)
    orch.apply_snapshot("prod", [blocked()])
    keys = [o["key"] for o in orch.answer_options(AgentKey("prod", "p0"), PROMPT)]
    assert keys == ["1", "3"]


def test_answer_options_mark_actions_that_need_confirmation():
    server = ServerConfig(id="prod", url="ws://x", token="t")
    config = Config(
        servers=[server],
        profiles=dict(DEFAULT_PROFILES),
        overview_order=["prod"],
        grid=(5, 3),
        safety=SafetyConfig(require_confirm_for=["deny"]),
    )
    orch = Orchestrator(config, slots=13)
    orch.apply_snapshot("prod", [blocked()])
    confirm = {o["key"]: o["confirm"] for o in orch.answer_options(AgentKey("prod", "p0"), PROMPT)}
    assert confirm == {"1": False, "2": False, "3": True}


# --- sanitizing ---------------------------------------------------------------


def test_sanitize_prompt_strips_escapes_and_controls_but_keeps_layout():
    raw = "\x1b[1mBold\x1b[0m\tcol\r\nnext\x07line\x1b]0;title\x07\x00end"
    assert sanitize_prompt(raw) == "Bold\tcol\nnextline\x00end".replace("\x00", "")


def test_sanitize_prompt_keeps_the_tail_of_a_huge_prompt():
    raw = "x" * 100_000 + "\n1. Yes"
    out = sanitize_prompt(raw)
    assert len(out) <= 64 * 1024
    assert out.endswith("1. Yes")


# --- detail -------------------------------------------------------------------


def test_detail_carries_the_full_prompt_options_and_header():
    app, src, runner, clock = blocked_with_prompt()
    clock[0] += 42
    detail = src.card_detail("prod", "p0")
    assert detail["prompt"] == PROMPT
    assert detail["status"] == "blocked"
    assert detail["agent_type"] == "claude"
    assert (detail["repo"], detail["branch"], detail["workspace"], detail["tab"]) == (
        "herdeck",
        "main",
        "ws",
        "tab",
    )
    assert detail["since_s"] == 42
    assert detail["revision"] == decision_revision("prod", "p0", "term-1", PROMPT)
    assert [o["key"] for o in detail["options"]] == ["1", "2", "3"]
    assert detail["connected"] is True
    assert detail["stop_confirm"] is True  # act_force needs a second press by default
    assert "token" not in json.dumps(detail)


def test_detail_without_a_cached_prompt_reports_pending_and_rereads():
    app, src, runner, _ = make()
    src._on_snapshot("prod", [blocked()])
    runner.sent.clear()
    # The pre-read is still out: nothing to show, no options offered yet.
    detail = src.card_detail("prod", "p0", refresh=True)
    assert detail["prompt"] is None
    assert detail["prompt_pending"] is True
    assert detail["options"] == []
    reads = [m for m in runner.sent if m["type"] == "read"]
    assert len(reads) == 1 and reads[0]["pane_id"] == "p0"
    # ... and that fresh read populates the card.
    src._on_result("prod", reads[0]["req"], {"text": PROMPT, "pane_id": "p0"})
    assert src.card_detail("prod", "p0")["prompt"] == PROMPT


def test_detail_of_a_working_agent_has_no_prompt_or_options():
    app, src, runner, _ = make()
    src._on_snapshot("prod", [AgentState(AgentKey("prod", "p0"), "codex", "p0", Status.WORKING)])
    detail = src.card_detail("prod", "p0")
    assert detail["status"] == "working"
    assert detail["prompt"] is None and detail["prompt_pending"] is False
    assert detail["options"] == []
    assert detail["can_text"] is True


def test_detail_unknown_agent_is_none():
    app, src, runner, _ = make()
    assert src.card_detail("prod", "nope") is None


def test_resolve_tile_index_to_agent():
    app, src, runner, clock = make()
    src._on_snapshot("prod", [blocked()])
    app.refresh()
    assert src.card_resolve(0) is None  # just re-occupied: the press guard holds
    clock[0] += 1
    assert src.card_resolve(0) == ("prod", "p0")
    assert src.card_resolve(7) is None


# --- answer -------------------------------------------------------------------


def test_answer_sends_the_drill_keys_with_the_blocked_and_identity_guards():
    app, src, runner, _ = blocked_with_prompt(reply=lambda m: {"sent": True})
    rev = src.card_detail("prod", "p0")["revision"]
    out = src.card_answer("prod", "p0", "3", rev)
    assert out == {"ok": True, "code": "sent", "message": ""}
    acts = [m for m in runner.sent if m["type"] == "act"]
    assert len(acts) == 1
    assert acts[0]["keys"] == ["3", "enter"]
    assert acts[0]["guard"] is True
    assert acts[0]["terminal_id"] == "term-1"


def test_answer_fallback_sends_the_profile_keys():
    app, src, runner, _ = make(reply=lambda m: {"sent": True})
    src._on_snapshot("prod", [blocked()])
    cache_prompt(src, runner, text="Continue? (y/n)")
    rev = src.card_detail("prod", "p0")["revision"]
    out = src.card_answer("prod", "p0", "deny", rev)
    assert out["ok"] is True
    act = [m for m in runner.sent if m["type"] == "act"][-1]
    assert act["keys"] == list(DEFAULT_PROFILES["claude"].deny)


def test_answer_with_a_stale_revision_is_refused_without_sending():
    app, src, runner, _ = blocked_with_prompt(reply=lambda m: {"sent": True})
    out = src.card_answer("prod", "p0", "1", "not-the-shown-prompt")
    assert out["ok"] is False and out["code"] == "stale"
    assert not [m for m in runner.sent if m["type"] == "act"]


def test_answer_after_the_prompt_changed_in_place_is_stale():
    app, src, runner, _ = blocked_with_prompt(reply=lambda m: {"sent": True})
    seen = src.card_detail("prod", "p0")["revision"]
    # A drill read replaces the cached prompt for the same block episode.
    src.card_detail("prod", "p0", refresh=True)
    read = [m for m in runner.sent if m["type"] == "read"][-1]
    src._on_result("prod", read["req"], {"text": "Other?\n1. A\n2. B", "pane_id": "p0"})
    out = src.card_answer("prod", "p0", "1", seen)
    assert out["code"] == "stale"


def test_answer_on_an_agent_that_is_no_longer_blocked_is_refused():
    app, src, runner, _ = blocked_with_prompt(reply=lambda m: {"sent": True})
    rev = src.card_detail("prod", "p0")["revision"]
    src._on_event("prod", AgentState(AgentKey("prod", "p0"), "claude", "p0", Status.WORKING, terminal_id="term-1"))
    out = src.card_answer("prod", "p0", "1", rev)
    assert out["code"] == "not_blocked"
    assert not [m for m in runner.sent if m["type"] == "act"]


def test_answer_with_an_unknown_option_key_is_invalid():
    app, src, runner, _ = blocked_with_prompt(reply=lambda m: {"sent": True})
    rev = src.card_detail("prod", "p0")["revision"]
    assert src.card_answer("prod", "p0", "9", rev)["code"] == "invalid"


def test_answer_needing_confirmation_is_the_clients_job_but_flagged():
    app, src, runner, _ = blocked_with_prompt(confirm=("deny",))
    options = {o["key"]: o for o in src.card_detail("prod", "p0")["options"]}
    assert options["3"]["confirm"] is True


def test_bridge_skip_is_reported():
    app, src, runner, _ = blocked_with_prompt(reply=lambda m: {"skipped": True})
    rev = src.card_detail("prod", "p0")["revision"]
    assert src.card_answer("prod", "p0", "1", rev)["code"] == "not_blocked"


def test_identity_change_is_reported():
    app, src, runner, _ = blocked_with_prompt(
        reply=lambda m: {"skipped": True, "message": "agent identity changed"}
    )
    rev = src.card_detail("prod", "p0")["revision"]
    assert src.card_answer("prod", "p0", "1", rev)["code"] == "identity_changed"


def test_read_only_bridge_error_is_surfaced_not_swallowed():
    app, src, runner, _ = blocked_with_prompt(
        reply=lambda m: Error("read-only token: 'act' is not allowed", req=m["req"])
    )
    rev = src.card_detail("prod", "p0")["revision"]
    out = src.card_answer("prod", "p0", "1", rev)
    assert out["ok"] is False
    assert out["code"] == "readonly"
    assert "read-only" in out["message"]


def test_reqless_bridge_error_fails_the_pending_card_request():
    app, src, runner, _ = blocked_with_prompt(reply=lambda m: Error("boom", req=None))
    rev = src.card_detail("prod", "p0")["revision"]
    out = src.card_answer("prod", "p0", "1", rev)
    assert out == {"ok": False, "code": "error", "message": "boom"}


def test_silent_bridge_reports_pending_after_the_wait(monkeypatch):
    monkeypatch.setattr("herdeck.deckapp.agent_card.CARD_REPLY_TIMEOUT_S", 0.05)
    app, src, runner, _ = blocked_with_prompt(reply=lambda m: None)
    rev = src.card_detail("prod", "p0")["revision"]
    assert src.card_answer("prod", "p0", "1", rev)["code"] == "pending"


def test_disconnected_server_refuses_without_sending():
    app, src, runner, _ = blocked_with_prompt(reply=lambda m: {"sent": True})
    rev = src.card_detail("prod", "p0")["revision"]
    src._on_connection("prod", False)
    assert src.card_answer("prod", "p0", "1", rev)["code"] == "disconnected"
    assert not [m for m in runner.sent if m["type"] == "act"]


def test_connection_drop_fails_a_waiting_request():
    holder = {}

    def reply(msg):
        # Deliver the drop from another thread while the card request waits.
        threading.Timer(0.05, lambda: holder["src"]._on_connection("prod", False)).start()
        return None

    app, src, runner, _ = blocked_with_prompt(reply=reply)
    holder["src"] = src
    rev = src.card_detail("prod", "p0")["revision"]
    assert src.card_answer("prod", "p0", "1", rev)["code"] == "disconnected"


# --- text / stop / focus ------------------------------------------------------


def test_text_uses_send_text_like_herdeck_ctl():
    app, src, runner, _ = make(reply=lambda m: {"sent": True})
    src._on_snapshot("prod", [blocked()])
    runner.sent.clear()
    out = src.card_text("prod", "p0", "use the staging db\n")
    assert out["ok"] is True
    msg = [m for m in runner.sent if m["type"] == "send_text"][0]
    assert msg["text"] == "use the staging db"
    assert msg["terminal_id"] == "term-1"


@pytest.mark.parametrize("text", ["", "   \n", "x" * 5000])
def test_text_rejects_empty_and_oversized(text):
    app, src, runner, _ = make(reply=lambda m: {"sent": True})
    src._on_snapshot("prod", [blocked()])
    runner.sent.clear()
    assert src.card_text("prod", "p0", text)["code"] == "invalid"
    assert not [m for m in runner.sent if m["type"] == "send_text"]


def test_stop_sends_the_profile_stop_keys_unguarded():
    app, src, runner, _ = make(reply=lambda m: {"sent": True})
    src._on_snapshot("prod", [blocked()])
    runner.sent.clear()
    assert src.card_stop("prod", "p0")["ok"] is True
    act = [m for m in runner.sent if m["type"] == "act"][0]
    assert act["guard"] is False
    assert act["keys"] == list(DEFAULT_PROFILES["claude"].stop)


def test_focus_sends_focus_and_reports_focused():
    app, src, runner, _ = make(reply=lambda m: {"focused": True})
    src._on_snapshot("prod", [blocked()])
    runner.sent.clear()
    out = src.card_focus("prod", "p0")
    assert out["ok"] is True and out["code"] == "focused"
    assert [m["type"] for m in runner.sent if m["type"] != "list"] == ["focus"]


# --- protocol: the read-only error names its request -------------------------


def test_error_frame_keeps_its_request_id():
    msg = decode_inbound(json.dumps({"type": "error", "req": "r7", "message": "nope"}))
    assert msg == Error("nope", req="r7")
    assert decode_inbound(json.dumps({"type": "error", "message": "x"})).req is None


# --- HTTP routes --------------------------------------------------------------


def _serve(src, clock=None):
    return DeckApp(
        src, host="127.0.0.1", port=0, serve=True, icon_provider=StubIcons(), clock=clock
    )


def _serving_live(reply=None):
    server = ServerConfig(id="prod", url="ws://bridge.local:8765", token="t")
    config = Config(servers=[server], profiles=dict(DEFAULT_PROFILES), overview_order=["prod"], grid=(5, 3))
    src = LiveSource(config, server)
    runner = ReplyingRunner(reply)
    runner.src = src
    src.attach_runner(runner, "prod")
    clock = [1000.0]
    app = _serve(src, clock=lambda: clock[0])
    src._on_connection("prod", True)
    src._on_snapshot("prod", [blocked()])
    cache_prompt(src, runner)
    app.refresh()
    clock[0] += 1  # past the tile-change guard
    return app, src, runner


def _get(app, path, token=None):
    sep = "&" if "?" in path else "?"
    url = f"http://{app.host}:{app.port}{path}{sep}token={token if token is not None else app.token}"
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, json.loads(r.read())


def _post(app, path, body, token=None):
    req = urllib.request.Request(
        f"http://{app.host}:{app.port}{path}", data=json.dumps(body).encode(), method="POST"
    )
    req.add_header("X-Herdeck-Token", token if token is not None else app.token)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read() or b"null")


def test_routes_require_the_token():
    app, src, runner = _serving_live()
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(app, "/agent/detail?index=0", token="wrong")
        assert e.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(app, "/agent/text", {"server_id": "prod", "pane_id": "p0", "text": "x"}, token="no")
        assert e.value.code == 403
        assert not [m for m in runner.sent if m["type"] == "send_text"]
    finally:
        app.close()


def test_detail_route_by_index_then_by_key():
    app, src, runner = _serving_live()
    try:
        code, body = _get(app, "/agent/detail?index=0")
        assert code == 200
        assert (body["server_id"], body["pane_id"]) == ("prod", "p0")
        assert body["prompt"] == PROMPT
        code, body = _get(app, "/agent/detail?server_id=prod&pane_id=p0")
        assert body["pane_id"] == "p0"
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(app, "/agent/detail?index=9")
        assert e.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(app, "/agent/detail")
        assert e.value.code == 400
    finally:
        app.close()


def test_answer_text_stop_focus_routes():
    app, src, runner = _serving_live(reply=lambda m: {"focused": True} if m["type"] == "focus" else {"sent": True})
    try:
        _, detail = _get(app, "/agent/detail?server_id=prod&pane_id=p0")
        key = {"server_id": "prod", "pane_id": "p0"}
        code, out = _post(app, "/agent/answer", {**key, "key": "1", "revision": detail["revision"]})
        assert (code, out["code"]) == (200, "sent")
        code, out = _post(app, "/agent/text", {**key, "text": "hi"})
        assert out["ok"] is True
        code, out = _post(app, "/agent/stop", key)
        assert out["ok"] is True
        code, out = _post(app, "/agent/focus", key)
        assert out["code"] == "focused"
        types = [m["type"] for m in runner.sent if m["type"] != "list"]
        assert types[-4:] == ["act", "send_text", "act", "focus"]
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(app, "/agent/answer", {**key, "key": 1})
        assert e.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(app, "/agent/stop", {"server_id": "prod", "pane_id": "ghost"})
        assert e.value.code == 404
    finally:
        app.close()


def test_mock_source_has_no_agent_card():
    app = _serve(MockSource())
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(app, "/agent/detail?index=0")
        assert e.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(app, "/agent/text", {"server_id": "demo", "pane_id": "p0", "text": "x"})
        assert e.value.code == 404
    finally:
        app.close()
