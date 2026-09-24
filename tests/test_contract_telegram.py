"""Contract: Telegram alerts (one-way and interactive) as the running cockpit
delivers them.

The runtime is started through its production entry (``python -m herdeck.web
run``) against a fake bridge; the Telegram Bot API is a local fake reached via
the harness's ``sitecustomize`` redirect. The tests pin the Bot API calls
(sendMessage fields, inline keyboards, callback answers, reply routing) and the
bridge commands a Telegram tap produces, plus the authorization checks
(allowed_user_ids, chat_id, message_thread_id).
"""

from __future__ import annotations

import json
import time

import pytest
from contract_support import (
    PROMPT,
    FakeBridge,
    FakeTelegram,
    RuntimeProcess,
    base_env,
    pane,
    seed_web_token,
    wait_until,
    write_config,
)

CHAT = "-1001"
THREAD = 7
USER = 42

INTERACTIVE = f"""
[notifications]
enabled = true
backends = ["telegram"]
on = ["blocked", "done"]
sound = false

[notifications.telegram]
token_env = "HERDECK_CONTRACT_TG_TOKEN"
chat_id = "{CHAT}"
message_thread_id = {THREAD}
interactive = true
allowed_user_ids = [{USER}]
"""

ONE_WAY = f"""
[notifications]
enabled = true
backends = ["telegram"]
on = ["blocked", "done"]
sound = true

[notifications.telegram]
token_env = "HERDECK_CONTRACT_TG_TOKEN"
chat_id = "{CHAT}"
"""


def start(tmp_path, panes, config_extra):
    bridge = FakeBridge(panes)
    telegram = FakeTelegram()
    home = tmp_path / "home"
    home.mkdir()
    seed_web_token(home)
    config = write_config(home / "config.toml", bridge, extra=config_extra)
    env = base_env(home, telegram=telegram)
    env.update({"HERDECK_CONFIG": str(config), "HERDECK_WEB_PORT": "0"})
    proc = RuntimeProcess(["-m", "herdeck.web", "run"], env, cwd=home)
    try:
        proc.wait_line(r"listening on http://127\.0\.0\.1:\d+/ ")
        bridge.wait_connected()
        bridge.wait_message(lambda m: m.get("type") == "list")
    except BaseException:
        proc.stop()
        bridge.close()
        telegram.close()
        raise
    return proc, bridge, telegram


@pytest.fixture
def interactive(tmp_path):
    proc, bridge, telegram = start(tmp_path, [pane("p1", "working", label="alpha")], INTERACTIVE)
    try:
        # the inbound poller is running before any alert goes out
        telegram.wait_call("getUpdates")
        yield proc, bridge, telegram
    finally:
        proc.stop()
        bridge.close()
        telegram.close()


def _keyboard(fields):
    return json.loads(fields["reply_markup"])["inline_keyboard"]


def _alert(bridge, telegram):
    """Block p1 and return (sendMessage fields, callback token)."""
    time.sleep(0.3)  # the working snapshot is the baseline
    bridge.push_event(pane("p1", "blocked", label="alpha"))
    fields = telegram.wait_call("sendMessage", lambda f: "reply_markup" in f)
    token = _keyboard(fields)[0][0]["callback_data"].split(":")[1]
    return fields, token


def _callback(update_id, message_id, data, *, user=USER, chat=CHAT, thread=THREAD):
    message = {"message_id": message_id, "chat": {"id": int(chat)}}
    if thread is not None:
        message["message_thread_id"] = thread
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": user},
            "message": message,
            "data": data,
        },
    }


def _answer(telegram, update_id):
    return telegram.wait_call(
        "answerCallbackQuery", lambda f: f["callback_query_id"] == f"cb{update_id}"
    )


def test_blocked_alert_carries_prompt_and_answer_buttons(interactive):
    _proc, bridge, telegram = interactive
    fields, token = _alert(bridge, telegram)
    assert fields["chat_id"] == CHAT
    assert fields["message_thread_id"] == str(THREAD)
    assert fields["disable_notification"] == "true"  # sound = false
    text = fields["text"]
    assert text.startswith("claude blocked\n")
    assert text.startswith("claude blocked\nrepo-p1 · main · local:p1\n\n")
    assert f"Waiting for:\n{PROMPT}" in text
    assert text.endswith("Reply to this message to send text to the agent.")
    assert _keyboard(fields) == [
        [
            {"text": "Approve", "callback_data": f"h:{token}:approve"},
            {"text": "Deny", "callback_data": f"h:{token}:deny"},
            {"text": "Stop", "callback_data": f"h:{token}:stop"},
        ],
        [{"text": "Read again", "callback_data": f"h:{token}:read"}],
    ]
    read = bridge.wait_message(lambda m: m.get("type") == "read")
    assert read["pane_id"] == "p1" and read["terminal_id"] == "term-p1"
    # exactly one alert for one transition
    time.sleep(0.5)
    assert len([f for f in telegram.sent("sendMessage") if "reply_markup" in f]) == 1


def test_callback_authorization_is_enforced(interactive):
    _proc, bridge, telegram = interactive
    fields, token = _alert(bridge, telegram)
    message_id = 101  # the fake Bot API numbers messages from 101
    assert fields
    telegram.queue_update(_callback(1, message_id, f"h:{token}:approve", user=99))
    assert _answer(telegram, 1)["text"] == "not authorized"
    telegram.queue_update(_callback(2, message_id, f"h:{token}:approve", thread=8))
    assert _answer(telegram, 2)["text"] == "not authorized"
    telegram.queue_update(_callback(3, message_id, f"h:{token}:approve", chat="-999"))
    assert _answer(telegram, 3)["text"] == "not authorized"
    telegram.queue_update(_callback(4, message_id, "h:wrongtoken:approve"))
    assert _answer(telegram, 4)["text"] == "stale"
    telegram.queue_update(_callback(5, message_id + 7, f"h:{token}:approve"))
    assert _answer(telegram, 5)["text"] == "stale"
    time.sleep(0.3)
    assert bridge.messages("act") == []


def test_reply_read_again_status_and_approve(interactive):
    _proc, bridge, telegram = interactive
    _fields, token = _alert(bridge, telegram)
    message_id = 101

    # a reply to the alert is typed into the agent's pane
    telegram.queue_update(
        {
            "update_id": 10,
            "message": {
                "message_id": 500,
                "from": {"id": USER},
                "chat": {"id": int(CHAT)},
                "message_thread_id": THREAD,
                "text": "use the staging db",
                "reply_to_message": {"message_id": message_id},
            },
        }
    )
    sent = bridge.wait_message(lambda m: m.get("type") == "send_text")
    assert sent["text"] == "use the staging db" and sent["pane_id"] == "p1"
    reply = telegram.wait_call("sendMessage", lambda f: f.get("text") == "sent to local:p1")
    assert json.loads(reply["reply_parameters"]) == {"message_id": 500}
    assert reply["message_thread_id"] == str(THREAD)

    # /status lists the tracked alerts
    telegram.queue_update(
        {
            "update_id": 11,
            "message": {
                "message_id": 501,
                "from": {"id": USER},
                "chat": {"id": int(CHAT)},
                "message_thread_id": THREAD,
                "text": "/status",
            },
        }
    )
    telegram.wait_call("sendMessage", lambda f: f.get("text") == "tracked blocked alerts:\n- local:p1")

    # read again refreshes the alert in place
    reads = len(bridge.messages("read"))
    telegram.queue_update(_callback(12, message_id, f"h:{token}:read"))
    assert _answer(telegram, 12)["text"] == "refreshed"
    wait_until(lambda: len(bridge.messages("read")) > reads)
    edited = telegram.wait_call("editMessageText")
    assert edited["chat_id"] == CHAT and edited["message_id"] == str(message_id)
    assert PROMPT in edited["text"]

    # approve sends the profile's approve keys, guarded
    telegram.queue_update(_callback(13, message_id, f"h:{token}:approve"))
    assert _answer(telegram, 13)["text"] == "sent"
    act = bridge.wait_message(lambda m: m.get("type") == "act")
    assert act == {
        "type": "act",
        "req": act["req"],
        "pane_id": "p1",
        "keys": ["1", "enter"],
        "guard": True,
        "terminal_id": "term-p1",
    }
    # the answered alert is spent
    telegram.queue_update(_callback(14, message_id, f"h:{token}:deny"))
    assert _answer(telegram, 14)["text"] == "stale"
    telegram.queue_update(
        {
            "update_id": 15,
            "message": {
                "message_id": 502,
                "from": {"id": USER},
                "chat": {"id": int(CHAT)},
                "message_thread_id": THREAD,
                "text": "late",
                "reply_to_message": {"message_id": message_id},
            },
        }
    )
    telegram.wait_call("sendMessage", lambda f: f.get("text") == "alert is stale")


def test_stop_needs_a_second_tap(interactive):
    _proc, bridge, telegram = interactive
    _fields, token = _alert(bridge, telegram)
    telegram.queue_update(_callback(20, 101, f"h:{token}:stop"))
    assert _answer(telegram, 20)["text"] == "confirmation required"
    time.sleep(0.3)
    assert bridge.messages("act") == []
    telegram.queue_update(_callback(21, 101, f"h:{token}:stop"))
    assert _answer(telegram, 21)["text"] == "sent"
    act = bridge.wait_message(lambda m: m.get("type") == "act")
    assert act["keys"] == ["ctrl+c"] and act["guard"] is False


def test_done_alert_is_one_way(interactive):
    _proc, bridge, telegram = interactive
    time.sleep(0.3)
    bridge.push_event(pane("p1", "done", label="alpha"))
    fields = telegram.wait_call("sendMessage")
    assert "reply_markup" not in fields
    assert fields["text"] == "claude · done\nrepo-p1 · main"
    assert fields["chat_id"] == CHAT and fields["message_thread_id"] == str(THREAD)


def test_one_way_backend_sends_plain_alerts_and_never_polls(tmp_path):
    proc, bridge, telegram = start(tmp_path, [pane("p1", "working", label="alpha")], ONE_WAY)
    try:
        time.sleep(0.3)
        bridge.push_event(pane("p1", "blocked", label="alpha"))
        fields = telegram.wait_call("sendMessage")
        assert fields["chat_id"] == CHAT
        assert "message_thread_id" not in fields
        assert "reply_markup" not in fields
        assert fields["disable_notification"] == "false"  # sound = true
        assert fields["text"] == "claude · needs input\nrepo-p1 · main"
        time.sleep(0.5)
        assert telegram.sent("getUpdates") == []
    finally:
        proc.stop()
        bridge.close()
        telegram.close()


def test_agent_blocked_before_start(tmp_path):
    """An agent already blocked when the runtime starts (e.g. a launchd restart)."""
    proc, bridge, telegram = start(tmp_path, [pane("p1", "blocked", label="alpha")], INTERACTIVE)
    try:
        telegram.wait_call("getUpdates")
        time.sleep(1.5)
        alerts = [f for f in telegram.sent("sendMessage") if "reply_markup" in f]
        assert len(alerts) == 1
    finally:
        proc.stop()
        bridge.close()
        telegram.close()
