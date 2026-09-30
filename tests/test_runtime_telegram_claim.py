"""A runtime-sent interactive Telegram alert answers only the question it
showed: a tap or reply on an older message of the same blocked episode is
refused as stale (the same rule as a bridge-sent alert, test_bridge_control).

Wiring: LiveSource state -> RuntimeAgentControl (as RuntimeServices builds it)
-> TelegramInteractor with a fake bot; bridge replies are simulated in-process.
"""

from __future__ import annotations

from herdeck.app_control import RuntimeAgentControl
from herdeck.commands import command_to_msg
from herdeck.model import AgentKey, Status
from herdeck.telegram import TelegramInteractor
from tests.test_deckapp_live import agent
from tests.test_runtime_events import EP, blocked, ev, make, subscribe

Q1 = "Allow edit?\n1. Yes\n2. No"
Q2 = "Run tests?\n1. Yes\n2. No"
R1, R2 = "1111111111111111", "2222222222222222"


class FakeBot:
    def __init__(self):
        self.sent, self.answers = [], []

    def send_message(self, **fields):
        self.sent.append(fields)
        return {"message_id": 100 + len(self.sent)}

    def answer_callback_query(self, cb_id, *, text=""):
        self.answers.append(text)

    def edit_message_text(self, **fields):
        return {}


class Rig:
    """RuntimeAgentControl bound to a LiveSource like RuntimeServices binds it;
    the bridge answers a read with ``self.screen`` and acks every answer."""

    def __init__(self, src, runner):
        self.src, self.runner, self.screen = src, runner, Q1
        self.control = RuntimeAgentControl(
            src._config,
            send=self._send,
            current_agent=src.semantic_agent,
            answer_claim=src.telegram_answer_claim,
            claim_stale=src.telegram_claim_stale,
        )
        self.bot = FakeBot()
        self.tg = TelegramInteractor(self.bot, self.control, chat_id="-100",
                                     message_thread_id=None, allowed_user_ids=[7])

    async def _send(self, command, req):
        msg = command_to_msg(command, req)
        self.src._runners[command.server_id].send(msg)
        data = {"text": self.screen} if msg["type"] == "read" else {"sent": True}
        self.control.handle_result(req, data)

    def answers_from_telegram(self):
        return [m for m in self.runner.sent
                if m["type"] in ("act", "send_text") and str(m.get("req", "")).startswith("tg")]


def tap(rig, action):
    token = rig.bot.sent[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
    return {"callback_query": {"id": "cb", "from": {"id": 7}, "data": f"h:{token}:{action}",
                               "message": {"message_id": 101, "chat": {"id": -100}}}}


def reply(text):
    return {"message": {"message_id": 500, "from": {"id": 7}, "chat": {"id": -100},
                        "text": text, "reply_to_message": {"message_id": 101}}}


def deck_answer(src, server_id):
    """What a deck press sends (orchestrator command through the runner)."""
    src._runners[server_id].send(
        {"type": "act", "req": "r1", "pane_id": "p0", "keys": ["1"], "guard": True}
    )


# --- a bridge with lifecycle events --------------------------------------------


def events_rig(tmp_path):
    src, server, runner = make(tmp_path)
    subscribe(src, server)
    src._on_snapshot(server.id, [blocked(server.id)])
    src._on_lifecycle(server.id, ev("blocked", 1, prompt=Q1, revision=R1))
    return Rig(src, runner), server


async def alert(rig, server):
    state = rig.src.semantic_agent(AgentKey(server.id, "p0"))
    await rig.tg.notify_blocked(state, body="api", sound=False, multi_server=False)
    assert Q1 in rig.bot.sent[0]["text"]


async def test_events_bridge_old_alert_tap_is_stale_after_next_question(tmp_path):
    rig, server = events_rig(tmp_path)
    await alert(rig, server)
    deck_answer(rig.src, server.id)
    rig.src._on_lifecycle(server.id, ev("answered", 2, by="deck"))
    rig.screen = Q2
    rig.src._on_lifecycle(server.id, ev("blocked", 3, prompt=Q2, revision=R2))  # same episode
    for action in ("approve", "deny", "stop"):
        await rig.tg.process_update(tap(rig, action))
        assert rig.bot.answers[-1] == "stale"
    assert rig.answers_from_telegram() == []


async def test_events_bridge_old_alert_is_stale_before_the_answer_event_arrives(tmp_path):
    rig, server = events_rig(tmp_path)
    await alert(rig, server)
    deck_answer(rig.src, server.id)  # the bridge's "answered" has not come back yet
    await rig.tg.process_update(tap(rig, "approve"))
    assert rig.bot.answers[-1] == "stale"
    assert rig.answers_from_telegram() == []


async def test_events_bridge_tap_on_current_question_carries_the_claim(tmp_path):
    rig, server = events_rig(tmp_path)
    await alert(rig, server)
    rig.src._on_lifecycle(server.id, ev("blocked", 2, prompt=Q2, revision=R2))  # prompt moved on
    await rig.tg.process_update(tap(rig, "read"))  # the message now shows Q2
    await rig.tg.process_update(tap(rig, "approve"))
    assert rig.bot.answers[-1] == "sent"
    (sent,) = rig.answers_from_telegram()
    # the claimed question, not whatever is current when the stamp runs
    assert sent["episode_id"] == EP and sent["prompt_revision"] == R2


async def test_events_bridge_claim_is_not_overwritten_by_the_stamp(tmp_path):
    rig, server = events_rig(tmp_path)
    await alert(rig, server)
    claim = rig.src.telegram_answer_claim(AgentKey(server.id, "p0"))
    # the current revision moves on after the claim was checked: the message
    # still carries the claimed one, so the bridge's guard judges the claim
    rig.src._ev_revision[AgentKey(server.id, "p0")] = (EP, R2)
    rig.src._runners[server.id].send(
        {"type": "act", "req": "tg9", "pane_id": "p0", "keys": ["1"], "guard": True,
         **claim["wire"]}
    )
    assert rig.runner.sent[-1]["prompt_revision"] == R1


async def test_events_bridge_reply_to_old_alert_is_stale(tmp_path):
    rig, server = events_rig(tmp_path)
    await alert(rig, server)
    deck_answer(rig.src, server.id)
    rig.src._on_lifecycle(server.id, ev("answered", 2, by="deck"))
    rig.src._on_lifecycle(server.id, ev("blocked", 3, prompt=Q2, revision=R2))
    await rig.tg.process_update(reply("do it"))
    assert rig.bot.sent[-1]["text"] == "stale"
    assert rig.answers_from_telegram() == []


# --- an old bridge (no lifecycle events) -----------------------------------------


def old_rig(tmp_path):
    src, server, runner = make(tmp_path, events=False)
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.WORKING)])
    src._on_snapshot(server.id, [agent(server.id, "p0", Status.BLOCKED)])
    read = next(m for m in runner.sent if m["type"] == "read")
    src._on_result(read["req"], {"text": Q1, "pane_id": "p0"})  # the local pre-read
    return Rig(src, runner), server


async def test_old_bridge_old_alert_tap_is_stale_after_a_deck_answer(tmp_path):
    rig, server = old_rig(tmp_path)
    await alert(rig, server)
    deck_answer(rig.src, server.id)
    rig.screen = Q2
    for action in ("approve", "deny", "stop"):
        await rig.tg.process_update(tap(rig, action))
        assert rig.bot.answers[-1] == "stale"
    assert rig.answers_from_telegram() == []


async def test_old_bridge_old_alert_is_stale_when_the_pre_read_prompt_changed(tmp_path):
    rig, server = old_rig(tmp_path)
    await alert(rig, server)
    key = AgentKey(server.id, "p0")
    with rig.src._lock:
        rig.src._preread[key] = Q2  # answered elsewhere; the prompt moved on
    await rig.tg.process_update(tap(rig, "approve"))
    assert rig.bot.answers[-1] == "stale"
    assert rig.answers_from_telegram() == []


async def test_old_bridge_tap_on_current_question_passes_without_a_wire_claim(tmp_path):
    rig, server = old_rig(tmp_path)
    await alert(rig, server)
    await rig.tg.process_update(tap(rig, "approve"))
    assert rig.bot.answers[-1] == "sent"
    (sent,) = rig.answers_from_telegram()
    assert "episode_id" not in sent  # a local block episode never goes on the wire


async def test_old_bridge_reply_to_old_alert_is_stale(tmp_path):
    rig, server = old_rig(tmp_path)
    await alert(rig, server)
    deck_answer(rig.src, server.id)
    await rig.tg.process_update(reply("do it"))
    assert rig.bot.sent[-1]["text"] == "stale"
    assert rig.answers_from_telegram() == []


# --- callers without a claim keep today's behaviour -------------------------------


async def test_cockpit_and_deck_answers_carry_no_claim(tmp_path):
    rig, server = events_rig(tmp_path)
    key = AgentKey(server.id, "p0")
    result = await rig.control.approve(key)  # the cockpit API path: no claim
    assert result.sent
    sent = rig.answers_from_telegram()[-1]
    assert sent["episode_id"] == EP and sent["prompt_revision"] == R1  # stamped as before


def test_runtime_services_control_reads_claims_from_the_current_source(tmp_path):
    from herdeck.deckapp.services import RuntimeServices

    rig, server = events_rig(tmp_path)
    services = RuntimeServices(rig.src.config, current_source=lambda: rig.src,
                               getenv=lambda name: None)
    try:
        key = AgentKey(server.id, "p0")
        claim = services.control.answer_claim(key)
        assert claim["wire"] == {"episode_id": EP, "prompt_revision": R1}
        assert services.control._stale(key, claim) is False
        deck_answer(rig.src, server.id)
        assert services.control._stale(key, claim) is True
    finally:
        services.close()
