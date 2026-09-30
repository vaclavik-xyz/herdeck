"""BridgeAgentControl: Telegram control acting through the bridge's answer guard."""

from __future__ import annotations

import asyncio
import functools

from herdeck.bridge import StubHerdr, _wire_panes
from herdeck.bridge_answers import execute_answer
from herdeck.bridge_notify import BridgeAgentControl
from herdeck.bridge_settings import BridgeSettingsStore
from herdeck.events import EventHub
from herdeck.model import AgentKey, AgentState, Status
from herdeck.status_since import StatusSinceTracker

PROMPT = "Allow edit?\n1. Yes\n2. No"
KEY = AgentKey("srv", "w1:p1")


def raw_pane(status="blocked"):
    return {
        "pane_id": "w1:p1",
        "workspace_id": "w1",
        "cwd": "/tmp/api",
        "foreground_cwd": "/tmp/api",
        "agent_status": status,
        "agent": "claude",
        "terminal_id": "t1",
    }


def agent(status=Status.BLOCKED, backend="herdr"):
    return AgentState(KEY, "claude", "api", status, terminal_id="t1", backend=backend)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


async def make(tmp_path, settings=None, status="blocked", agents=None, read_prompt=None):
    herdr = StubHerdr(panes=[raw_pane(status)])
    herdr.detection["w1:p1"] = PROMPT
    since = StatusSinceTracker(None)
    hub = EventHub(herdr, "srv", clock=lambda: 1000.0, epoch="e1")
    panes = _wire_panes(herdr.panes)
    since.stamp(panes)
    EventHub.stamp(panes)
    hub.observe(panes)
    for _ in range(5):
        await asyncio.sleep(0)
    store = BridgeSettingsStore(tmp_path / "s.toml")
    if settings is not None:
        assert store.put(0, settings, "t").ok
    state = {KEY: agent()} if agents is None else agents
    calls = []

    async def fallback_read(pane_id):
        calls.append(pane_id)
        return "fallback text"

    clock = Clock()
    control = BridgeAgentControl(
        execute=functools.partial(execute_answer, herdr, "srv", events=hub),
        agents=lambda: state,
        episodes=hub,
        settings=store,
        read_prompt=read_prompt or fallback_read,
        clock=clock,
    )
    control.clock, control.fallback_calls = clock, calls
    return herdr, hub, control


async def test_approve_uses_shared_profile_keys_and_episode(tmp_path):
    herdr, hub, control = await make(
        tmp_path, {"answer_profiles": {"claude": {"approve": ["y", "Enter"], "deny": ["n"], "stop": ["C-c"], "approve_always": ["a"]}}}
    )
    result = await control.approve(KEY)
    assert result.sent and not result.skipped
    assert herdr.sent == [("w1:p1", ["y", "Enter"])]
    answered = [e for e in hub.events() if e["kind"] == "answered"]
    assert len(answered) == 1 and answered[0]["by"] == "telegram"
    await hub.close()


async def test_deny_uses_default_profile_when_settings_unset(tmp_path):
    herdr, hub, control = await make(tmp_path)
    result = await control.deny(KEY)
    assert result.sent
    assert herdr.sent[0][0] == "w1:p1" and herdr.sent[0][1]
    await hub.close()


async def test_answer_message_carries_episode_id_and_revision(tmp_path):
    herdr, hub, _ = await make(tmp_path)
    seen = []

    async def spy(msg, by):
        seen.append((msg, by))
        return {"sent": True}

    control = BridgeAgentControl(
        execute=spy,
        agents=lambda: {KEY: agent()},
        episodes=hub,
        settings=BridgeSettingsStore(tmp_path / "x.toml"),
        read_prompt=lambda pane: asyncio.sleep(0, result=None),
    )
    ep = hub.open_episode("w1:p1")
    assert (await control.approve(KEY)).sent
    msg, by = seen[0]
    assert by == "telegram" and msg["type"] == "act" and msg["guard"] is True
    assert msg["episode_id"] == ep.id and msg["prompt_revision"] == ep.revision
    assert msg["terminal_id"] == "t1"
    await hub.close()


async def test_second_answer_to_answered_episode_is_stale(tmp_path):
    herdr, hub, control = await make(tmp_path)
    assert (await control.approve(KEY)).sent
    again = await control.approve(KEY)
    assert again.skipped and not again.sent and again.message == "stale"
    assert len(herdr.sent) == 1
    await hub.close()


async def test_deck_answer_first_makes_telegram_stale(tmp_path):
    herdr, hub, control = await make(tmp_path)
    ep = hub.open_episode("w1:p1")
    msg = {"type": "act", "req": "r", "pane_id": "w1:p1", "keys": ["1"], "episode_id": ep.id}
    assert (await execute_answer(herdr, "srv", msg, "deck", events=hub)) == {"sent": True}
    result = await control.deny(KEY)
    assert result.skipped and result.message == "stale"
    assert len(herdr.sent) == 1
    await hub.close()


async def test_stop_needs_confirm_then_sends_within_ttl(tmp_path):
    herdr, hub, control = await make(tmp_path, {"safety": {"require_confirm_for": ["act_force"]}})
    first = await control.stop(KEY)
    assert not first.sent and not first.skipped and first.message == "confirmation required"
    assert herdr.sent == []
    control.clock.now += 59
    second = await control.stop(KEY)
    assert second.sent
    assert len(herdr.sent) == 1
    await hub.close()


async def test_confirm_expires_after_60s_and_reset_disarms(tmp_path):
    herdr, hub, control = await make(tmp_path)
    assert (await control.stop(KEY)).message == "confirmation required"
    control.clock.now += 61
    assert (await control.stop(KEY)).message == "confirmation required"
    assert herdr.sent == []
    control.reset_confirmation(KEY)
    assert (await control.stop(KEY)).message == "confirmation required"
    assert (await control.stop(KEY, confirmed=True)).sent
    await hub.close()


async def test_stop_without_confirm_requirement_sends(tmp_path):
    herdr, hub, control = await make(tmp_path, {"safety": {"require_confirm_for": []}})
    assert (await control.stop(KEY)).sent
    assert len(herdr.sent) == 1
    await hub.close()


async def test_send_text_reaches_herdr_with_episode(tmp_path):
    herdr, hub, control = await make(tmp_path)
    result = await control.send_text(KEY, "hello")
    assert result.sent
    assert ("w1:p1", "hello") in herdr.sent
    assert (await control.send_text(KEY, "again")).skipped
    await hub.close()


async def test_unknown_agent_and_non_herdr_backend(tmp_path):
    herdr, hub, control = await make(tmp_path, agents={})
    assert control.current_agent(KEY) is None
    assert (await control.approve(KEY)).message == "agent is no longer available"
    assert (await control.send_text(KEY, "x")).message == "agent is no longer available"
    assert await control.read_prompt(KEY) == ""
    _, hub2, control2 = await make(tmp_path, agents={KEY: agent(backend="t3")})
    assert not (await control2.approve(KEY)).sent
    await hub.close()
    await hub2.close()


async def test_read_prompt_prefers_episode_then_falls_back(tmp_path):
    herdr, hub, control = await make(tmp_path)
    assert await control.read_prompt(KEY) == hub.open_episode("w1:p1").prompt
    assert control.fallback_calls == []
    _, hub2, control2 = await make(tmp_path, status="working")
    assert await control2.read_prompt(KEY) == "fallback text"
    assert control2.fallback_calls == ["w1:p1"]
    await hub.close()
    await hub2.close()


async def test_execute_error_becomes_unsent_result(tmp_path):
    herdr, hub, _ = await make(tmp_path)

    async def boom(msg, by):
        return {"error": "pane gone"}

    control = BridgeAgentControl(
        execute=boom,
        agents=lambda: {KEY: agent()},
        episodes=hub,
        settings=BridgeSettingsStore(tmp_path / "y.toml"),
        read_prompt=lambda pane: asyncio.sleep(0, result=None),
    )
    result = await control.approve(KEY)
    assert not result.sent and not result.skipped and result.message == "pane gone"
    await hub.close()


# --- a Telegram alert pins the question it showed -----------------------------

Q2 = "Run tests?\n1. Yes\n2. No"


class FakeBot:
    """The Bot API calls TelegramInteractor makes, recorded."""

    def __init__(self):
        self.sent, self.answers = [], []

    def send_message(self, **fields):
        self.sent.append(fields)
        return {"message_id": 100 + len(self.sent)}

    def answer_callback_query(self, cb_id, *, text=""):
        self.answers.append(text)

    def edit_message_text(self, **fields):
        return {}


def interactor(control, bot):
    from herdeck.telegram import TelegramInteractor

    return TelegramInteractor(bot, control, chat_id="-100", message_thread_id=None,
                              allowed_user_ids=[7])


def tap(bot, action):
    token = bot.sent[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
    return {"callback_query": {"id": "cb", "from": {"id": 7}, "data": f"h:{token}:{action}",
                               "message": {"message_id": 101, "chat": {"id": -100}}}}


def reply(text):
    return {"message": {"message_id": 500, "from": {"id": 7}, "chat": {"id": -100},
                        "text": text, "reply_to_message": {"message_id": 101}}}


async def deck_answers_q1_then_q2_appears(herdr, hub, *, reread=True):
    ep = hub.open_episode("w1:p1")
    msg = {"type": "act", "req": "r", "pane_id": "w1:p1", "keys": ["1"], "episode_id": ep.id,
           "prompt_revision": ep.revision}
    assert (await execute_answer(herdr, "srv", msg, "deck", events=hub)) == {"sent": True}
    herdr.detection["w1:p1"] = Q2
    if reread:
        await hub._fresh_read(ep)
        assert ep.revision != ep.answered_revision  # Q2 is the open question now


async def test_old_alert_button_does_not_answer_the_next_question(tmp_path):
    herdr, hub, control = await make(tmp_path)
    bot = FakeBot()
    tg = interactor(control, bot)
    await tg.notify_blocked(agent(), body="api", sound=False, multi_server=False)
    assert PROMPT in bot.sent[0]["text"]
    await deck_answers_q1_then_q2_appears(herdr, hub)  # Q2 not sent to Telegram
    for action in ("approve", "deny", "stop"):
        await tg.process_update(tap(bot, action))
        assert bot.answers[-1] == "stale"
    assert herdr.sent == [("w1:p1", ["1"])]  # only the deck's answer reached the pane
    await hub.close()


async def test_old_alert_is_stale_even_before_the_next_question_is_read(tmp_path):
    herdr, hub, control = await make(tmp_path)
    bot = FakeBot()
    tg = interactor(control, bot)
    await tg.notify_blocked(agent(), body="api", sound=False, multi_server=False)
    await deck_answers_q1_then_q2_appears(herdr, hub, reread=False)
    await tg.process_update(tap(bot, "approve"))
    assert bot.answers[-1] == "stale"
    assert herdr.sent == [("w1:p1", ["1"])]
    await hub.close()


async def test_reply_to_old_alert_does_not_reach_the_next_question(tmp_path):
    herdr, hub, control = await make(tmp_path)
    bot = FakeBot()
    tg = interactor(control, bot)
    await tg.notify_blocked(agent(), body="api", sound=False, multi_server=False)
    await deck_answers_q1_then_q2_appears(herdr, hub)
    await tg.process_update(reply("do it"))
    assert bot.sent[-1]["text"] == "stale"
    assert herdr.sent == [("w1:p1", ["1"])]  # the text never reached the pane
    await hub.close()


async def test_button_on_the_current_question_still_answers(tmp_path):
    herdr, hub, control = await make(tmp_path)
    bot = FakeBot()
    tg = interactor(control, bot)
    await tg.notify_blocked(agent(), body="api", sound=False, multi_server=False)
    await tg.process_update(tap(bot, "approve"))
    assert bot.answers[-1] == "sent"
    assert len(herdr.sent) == 1
    answered = [e for e in hub.events() if e["kind"] == "answered"]
    assert len(answered) == 1 and answered[0]["by"] == "telegram"
    await hub.close()


async def test_read_again_repins_the_alert_to_the_current_question(tmp_path):
    herdr, hub, control = await make(tmp_path)
    bot = FakeBot()
    tg = interactor(control, bot)
    await tg.notify_blocked(agent(), body="api", sound=False, multi_server=False)
    await deck_answers_q1_then_q2_appears(herdr, hub)
    await tg.process_update(tap(bot, "read"))  # the message now shows Q2
    await tg.process_update(tap(bot, "approve"))
    assert bot.answers[-1] == "sent"
    assert len(herdr.sent) == 2
    await hub.close()
