"""Actionable notification banners: click-to-drill and answering a blocked
agent from a banner (runtime side). The shell only ever talks to these routes;
every answer is re-validated against the agent's CURRENT block episode and
prompt, so a stale banner can never answer a later prompt."""

import json
import urllib.error
import urllib.request

import pytest

from herdeck.config import DEFAULT_PROFILES, SafetyConfig
from herdeck.deckapp import DeckApp
from herdeck.deckapp.live import LiveSource, sanitize_reply
from herdeck.model import AgentKey, Status
from herdeck.orchestrator import binary_answer
from tests.test_deckapp_live import FakeRunner, StubIcons, agent, live_config, make_live

CLAUDE_PROMPT = (
    "Do you want to make this edit to app.py?\n"
    "❯ 1. Yes\n"
    "  2. Yes, and don't ask again this session\n"
    "  3. No, and tell Claude what to do differently (esc)\n"
)


def _blocked_with_prompt(prompt=CLAUDE_PROMPT, *, connected=True):
    """A live app whose pane p0 is BLOCKED with its prompt pre-read."""
    app, src, server, runner = make_live()
    src._on_connection(server.id, connected)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    read = [m for m in runner.sent if m["type"] == "read"][-1]
    src._on_result(server.id, read["req"], {"text": prompt, "pane_id": "p0"})
    runner.sent.clear()
    key = AgentKey(server.id, "p0")
    return app, src, server, runner, key, src._block_episode[key]


# --- binary prompt detection (reuses the drill's option parsing) -------------


def test_permission_prompt_reduces_to_approve_and_deny():
    answer = binary_answer(CLAUDE_PROMPT, DEFAULT_PROFILES["claude"], SafetyConfig())
    assert (answer.approve, answer.deny) == ("1", "3")
    assert answer.sig


def test_a_real_question_is_not_binary():
    question = "Which colour?\n1. Red\n2. Blue\n"
    assert binary_answer(question, DEFAULT_PROFILES["claude"], SafetyConfig()) is None
    assert binary_answer("", DEFAULT_PROFILES["claude"], SafetyConfig()) is None
    # A prompt without numbered options (the drill's y/n fallback) is never
    # answered blind from a banner.
    assert binary_answer("Continue? (y/n)", DEFAULT_PROFILES["claude"], SafetyConfig()) is None


def test_actions_that_need_an_on_deck_confirmation_are_not_offered():
    safety = SafetyConfig(require_confirm_for=["act_force", "approve"])
    assert binary_answer(CLAUDE_PROMPT, DEFAULT_PROFILES["claude"], safety) is None


def test_the_signature_follows_the_option_list():
    other = CLAUDE_PROMPT.replace("app.py", "b.py").replace("1. Yes", "1. Yes!")
    a = binary_answer(CLAUDE_PROMPT, DEFAULT_PROFILES["claude"], SafetyConfig())
    b = binary_answer(other, DEFAULT_PROFILES["claude"], SafetyConfig())
    assert a.sig != b.sig


# --- click -> the agent's drill ---------------------------------------------


def test_banner_click_opens_that_agents_drill():
    app, src, server, runner = make_live()
    try:
        src._on_connection(server.id, True)
        src._on_snapshot(
            server.id,
            [agent(server.id, "p0", Status.WORKING), agent(server.id, "p1", Status.DONE)],
        )
        runner.sent.clear()
        assert app.open_agent(server.id, "p1") is True
        assert app._orch.drill_key() == AgentKey(server.id, "p1")
        assert [(m["type"], m["pane_id"]) for m in runner.sent] == [("focus", "p1"), ("read", "p1")]
        assert app.open_agent(server.id, "gone") is False
    finally:
        app.close()


# --- answering from a banner --------------------------------------------------


def test_approve_from_a_banner_sends_the_drills_keys_once():
    app, src, _server, runner, key, episode = _blocked_with_prompt()
    try:
        sig = binary_answer(CLAUDE_PROMPT, DEFAULT_PROFILES["claude"], SafetyConfig()).sig
        assert app.answer_agent(key.server_id, key.pane_id, episode, choice="approve", sig=sig) == "ok"
        [msg] = runner.sent
        assert msg["type"] == "act" and msg["guard"] is True
        assert msg["keys"] == ["1", "enter"] and msg["pane_id"] == "p0"
        # A second click (or a reminder banner of the same episode) is stale.
        assert app.answer_agent(key.server_id, key.pane_id, episode, choice="deny", sig=sig) == "stale"
        assert len(runner.sent) == 1
    finally:
        app.close()


def test_deny_sends_the_deny_option():
    app, src, _server, runner, key, episode = _blocked_with_prompt()
    try:
        sig = binary_answer(CLAUDE_PROMPT, DEFAULT_PROFILES["claude"], SafetyConfig()).sig
        assert src.answer_agent(key, episode, choice="deny", sig=sig) == "ok"
        assert runner.sent[0]["keys"] == ["3", "enter"]
    finally:
        app.close()


def test_a_banner_from_an_earlier_block_episode_is_stale():
    app, src, server, runner, key, episode = _blocked_with_prompt()
    try:
        sig = binary_answer(CLAUDE_PROMPT, DEFAULT_PROFILES["claude"], SafetyConfig()).sig
        # Answered on the deck, then a new prompt with the very same options.
        src._on_event(server.id, agent(server.id, "p0", Status.WORKING))
        src._on_event(server.id, agent(server.id, "p0", Status.BLOCKED))
        read = [m for m in runner.sent if m["type"] == "read"][-1]
        src._on_result(server.id, read["req"], {"text": CLAUDE_PROMPT, "pane_id": "p0"})
        runner.sent.clear()
        assert src.answer_agent(key, episode, choice="approve", sig=sig) == "stale"
        assert runner.sent == []
        # Not blocked at all any more -> stale as well.
        src._on_event(server.id, agent(server.id, "p0", Status.WORKING))
        assert src.answer_agent(key, src._block_episode.get(key, "x"), text="hi") == "stale"
        assert runner.sent == []
    finally:
        app.close()


def test_a_prompt_that_changed_in_place_is_stale():
    app, src, server, runner, key, episode = _blocked_with_prompt()
    try:
        sig = binary_answer(CLAUDE_PROMPT, DEFAULT_PROFILES["claude"], SafetyConfig()).sig
        # Same episode, but the drill's read now shows a different prompt.
        app.press(0)
        read = [m for m in runner.sent if m["type"] == "read"][-1]
        src._on_result(server.id, read["req"], {"text": "1. Yes\n2. No", "pane_id": "p0"})
        runner.sent.clear()
        assert src.answer_agent(key, episode, choice="approve", sig=sig) == "stale"
        assert runner.sent == []
    finally:
        app.close()


def test_reply_types_sanitized_text_into_the_pane():
    app, src, _server, runner, key, episode = _blocked_with_prompt()
    try:
        assert src.answer_agent(key, episode, text="  use the\x1b[31m other file‮ \n") == "ok"
        [msg] = runner.sent
        assert msg["type"] == "send_text"
        assert msg["text"] == "use the[31m other file"
        assert src.answer_agent(key, episode, text="again") == "stale"
    finally:
        app.close()


def test_malformed_answers_are_rejected():
    app, src, _server, runner, key, episode = _blocked_with_prompt()
    try:
        assert src.answer_agent(key, episode) == "invalid"
        assert src.answer_agent(key, episode, choice="approve", text="x") == "invalid"
        assert src.answer_agent(key, episode, choice="approve_always", sig="s") == "invalid"
        assert src.answer_agent(key, episode, text=" \x07 ") == "invalid"
        assert src.answer_agent(key, "", text="x") == "invalid"
        assert src.answer_agent(AgentKey(key.server_id, "nope"), episode, text="x") == "unknown"
        assert runner.sent == []
    finally:
        app.close()


def test_an_offline_server_is_unavailable_not_answered():
    app, src, _server, runner, key, episode = _blocked_with_prompt(connected=False)
    try:
        assert src.answer_agent(key, episode, text="hello") == "unavailable"
        assert runner.sent == []
    finally:
        app.close()


def test_sanitize_reply_bounds_length():
    assert sanitize_reply(None) == ""
    assert sanitize_reply("a\r\nb\tc") == "a\nb\tc"
    assert len(sanitize_reply("x" * 5000)) == 2000


# --- HTTP routes ---------------------------------------------------------------


def _post(app, path, body, *, token=True):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Herdeck-Token"] = app.token
    req = urllib.request.Request(
        f"http://{app.host}:{app.port}{path}",
        data=json.dumps(body).encode(),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytest.fixture
def served():
    config, server = live_config()
    src = LiveSource(config, server)
    runner = FakeRunner()
    src.attach_runner(runner)
    app = DeckApp(src, serve=True, icon_provider=StubIcons())
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    read = [m for m in runner.sent if m["type"] == "read"][-1]
    src._on_result(server.id, read["req"], {"text": CLAUDE_PROMPT, "pane_id": "p0"})
    try:
        yield app, src, server, runner
    finally:
        app.close()


def test_http_drill_route(served):
    app, _src, server, _runner = served
    ref = {"server_id": server.id, "pane_id": "p0"}
    assert _post(app, "/agents/drill", ref, token=False) == 403
    assert _post(app, "/agents/drill", {"server_id": server.id}) == 400
    assert _post(app, "/agents/drill", {**ref, "pane_id": "gone"}) == 404
    assert _post(app, "/agents/drill", ref) == 204
    assert app._orch.drill_key() == AgentKey(server.id, "p0")


def test_http_answer_route(served):
    app, src, server, runner = served
    key = AgentKey(server.id, "p0")
    episode = src._block_episode[key]
    sig = binary_answer(CLAUDE_PROMPT, DEFAULT_PROFILES["claude"], SafetyConfig()).sig
    ref = {"server_id": server.id, "pane_id": "p0"}
    body = {**ref, "episode": episode, "choice": "approve", "sig": sig}
    runner.sent.clear()
    assert _post(app, "/agents/answer", body, token=False) == 403
    assert _post(app, "/agents/answer", {**body, "episode": 7}) == 400
    assert _post(app, "/agents/answer", {**body, "episode": "old"}) == 409
    assert runner.sent == []
    assert _post(app, "/agents/answer", body) == 204
    assert [m["type"] for m in runner.sent] == ["act"]
    assert _post(app, "/agents/answer", body) == 409


# --- banner_actions / banner_prompt: the alert waits for its prompt ------------


class _Recorder:
    def __init__(self):
        self.calls = []

    def notify(self, title, body, sound=False, icon=None, meta=None):
        self.calls.append((title, body, meta))


def _enriching_live(*, actions=True, prompt=False, lang="en", wait=0.5):
    config, server = live_config()
    config.notifications.enabled = True
    config.notifications.banner_actions = actions
    config.notifications.banner_prompt = prompt
    config.view.language = lang
    pending = []
    src = LiveSource(config, server, notify_schedule=pending.append, prompt_wait_s=wait)
    recorder = _Recorder()
    src._notifier = recorder
    runner = FakeRunner()
    src.attach_runner(runner)
    src._on_connection(server.id, True)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    src._on_event(server.id, agent(server.id, "p0", Status.BLOCKED))
    return src, server, runner, pending, recorder


def _land_prompt(src, server, runner, text):
    read = [m for m in runner.sent if m["type"] == "read"][-1]
    src._on_result(server.id, read["req"], {"text": text, "pane_id": "p0"})


def test_binary_blocked_banner_carries_approve_and_deny():
    src, server, runner, pending, recorder = _enriching_live()
    _land_prompt(src, server, runner, CLAUDE_PROMPT)
    [deliver] = pending
    deliver()
    [(title, body, meta)] = recorder.calls
    assert title == "claude · needs input" and body == "p0 · main"
    assert meta["actions"] == [
        {"id": "approve", "label": "Approve"},
        {"id": "deny", "label": "Deny"},
    ]
    assert meta["sig"] == binary_answer(CLAUDE_PROMPT, DEFAULT_PROFILES["claude"], SafetyConfig()).sig
    assert meta["episode"] == src._block_episode[AgentKey(server.id, "p0")]
    assert "reply" not in meta


def test_open_question_banner_offers_a_reply_field_in_the_deck_language():
    src, server, runner, pending, recorder = _enriching_live(lang="cs")
    _land_prompt(src, server, runner, "Which colour?\n1. Red\n2. Blue\n")
    pending[0]()
    meta = recorder.calls[0][2]
    assert meta["reply"] == "Odpověz agentovi…"
    assert "actions" not in meta


def test_banner_prompt_appends_a_sanitized_excerpt():
    src, server, runner, pending, recorder = _enriching_live(actions=False, prompt=True)
    _land_prompt(src, server, runner, "Run \x1b[1mrm -rf build\x1b[0m?‮\n1. Yes\n2. No")
    pending[0]()
    title, body, meta = recorder.calls[0]
    assert body == "p0 · main\nRun rm -rf build?"
    assert "actions" not in meta and "reply" not in meta


def test_an_alert_whose_episode_ended_while_waiting_is_dropped():
    src, server, runner, pending, recorder = _enriching_live()
    src._on_event(server.id, agent(server.id, "p0", Status.WORKING))  # answered on the deck
    pending[0]()
    assert recorder.calls == []


def test_a_prompt_that_never_arrives_still_alerts_without_buttons():
    src, _server, _runner, pending, recorder = _enriching_live(wait=0.01)
    pending[0]()
    [(_title, body, meta)] = recorder.calls
    assert body == "p0 · main"
    assert "actions" not in meta and meta["reply"]  # replying needs no parsed prompt


def test_banner_opt_ins_parse_default_off_and_validate():
    from herdeck.config import ConfigError, parse_notifications
    from herdeck.settings import _notifications_config

    for parse in (parse_notifications, _notifications_config):
        n = parse({})
        assert (n.banner_actions, n.banner_prompt) == (False, False)
        n = parse({"banner_actions": True, "banner_prompt": True})
        assert (n.banner_actions, n.banner_prompt) == (True, True)
        with pytest.raises(ConfigError, match="notifications.banner_prompt"):
            parse({"banner_prompt": "yes"})


def test_without_the_opt_ins_the_alert_does_not_wait():
    src, _server, _runner, pending, recorder = _enriching_live(actions=False, wait=30)
    pending[0]()  # would block for 30 s if it waited for the prompt
    [(_title, _body, meta)] = recorder.calls
    assert "actions" not in meta and "reply" not in meta
