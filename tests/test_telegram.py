from herdeck.model import AgentKey, AgentState, Status


def test_request_json_http_error_with_non_json_body_raises_telegram_api_error(monkeypatch):
    import io
    import urllib.error

    import pytest

    from herdeck.telegram import TelegramApiError, _request_json

    def raise_http_error(*args, **kwargs):
        raise urllib.error.HTTPError(
            url="https://api.telegram.org/botTOK/sendMessage",
            code=502,
            msg="Bad Gateway",
            hdrs={},
            fp=io.BytesIO(b"bad gateway"),
        )

    monkeypatch.setattr("urllib.request.urlopen", raise_http_error)

    with pytest.raises(TelegramApiError) as excinfo:
        _request_json("TOK", "sendMessage", {})

    assert excinfo.value.error_code == 502
    assert "Bad Gateway" in excinfo.value.description


def test_bot_client_send_message_payload_includes_topic_and_markup():
    from herdeck.telegram import TelegramBotClient

    calls = []
    client = TelegramBotClient(
        "TOK", request=lambda method, fields: calls.append((method, fields)) or {"message_id": 9}
    )

    result = client.send_message(
        chat_id="-1001",
        text="body",
        sound=False,
        message_thread_id=456,
        reply_markup={"inline_keyboard": [[{"text": "Approve", "callback_data": "h:a:approve"}]]},
    )

    assert result == {"message_id": 9}
    assert calls == [
        (
            "sendMessage",
            {
                "chat_id": "-1001",
                "text": "body",
                "disable_notification": "true",
                "message_thread_id": "456",
                "reply_markup": '{"inline_keyboard":[[{"text":"Approve","callback_data":"h:a:approve"}]]}',
            },
        )
    ]


def test_bot_client_get_updates_uses_allowed_updates():
    from herdeck.telegram import TelegramBotClient

    calls = []
    client = TelegramBotClient("TOK", request=lambda method, fields: calls.append((method, fields)) or [])

    client.get_updates(offset=12, timeout=20)

    assert calls == [
        (
            "getUpdates",
            {
                "offset": "12",
                "timeout": "20",
                "allowed_updates": '["message","callback_query"]',
            },
        )
    ]


def test_bot_client_send_message_uses_reply_parameters_for_replies():
    from herdeck.telegram import TelegramBotClient

    calls = []
    client = TelegramBotClient("TOK", request=lambda method, fields: calls.append((method, fields)) or {})

    client.send_message(
        chat_id="-1001",
        text="reply",
        sound=True,
        reply_to_message_id=9,
    )

    assert calls == [
        (
            "sendMessage",
            {
                "chat_id": "-1001",
                "text": "reply",
                "disable_notification": "false",
                "reply_parameters": '{"message_id":9}',
            },
        )
    ]


def test_bot_client_answer_callback_and_edit_message_text_payloads():
    from herdeck.telegram import TelegramBotClient

    calls = []
    client = TelegramBotClient("TOK", request=lambda method, fields: calls.append((method, fields)) or True)

    client.answer_callback_query("cb1", text="sent")
    client.edit_message_text(chat_id="-1001", message_id=9, text="updated", message_thread_id=456)

    assert calls == [
        ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "sent"}),
        (
            "editMessageText",
            {
                "chat_id": "-1001",
                "message_id": "9",
                "text": "updated",
            },
        ),
    ]


def test_alert_formatter_truncates_prompt_and_builds_keyboard():
    from herdeck.telegram import TelegramAlertFormatter

    agent = AgentState(AgentKey("local", "p1"), "codex", "herdeck", Status.BLOCKED)
    agent.repo = "herdeck"
    agent.branch = "feat/tg"

    formatter = TelegramAlertFormatter(prompt_max_chars=12)
    text, markup = formatter.blocked_alert(
        agent, metadata_body="herdeck · feat/tg", prompt="0123456789abcdef", token="tok1"
    )

    assert "codex blocked" in text
    assert "local:p1" in text
    assert "0123456789ab..." in text
    assert markup["inline_keyboard"][0][0] == {
        "text": "Approve",
        "callback_data": "h:tok1:approve",
    }
    assert markup["inline_keyboard"][1][0] == {
        "text": "Read again",
        "callback_data": "h:tok1:read",
    }


def test_alert_formatter_empty_prompt_omits_blind_approve_and_deny_actions():
    from herdeck.telegram import TelegramAlertFormatter

    agent = AgentState(AgentKey("local", "p1"), "codex", "herdeck", Status.BLOCKED)

    text, markup = TelegramAlertFormatter().blocked_alert(
        agent, metadata_body="herdeck · feat/tg", prompt="  ", token="tok1"
    )

    assert "Prompt unavailable; use Read again." in text
    buttons = [button for row in markup["inline_keyboard"] for button in row]
    assert buttons == [
        {"text": "Stop", "callback_data": "h:tok1:stop"},
        {"text": "Read again", "callback_data": "h:tok1:read"},
    ]


def test_alert_store_maps_message_and_token_to_agent_and_expires():
    from herdeck.telegram import TelegramAlertStore

    key = AgentKey("local", "p1")
    store = TelegramAlertStore(now=lambda: 100.0, ttl_seconds=10.0, token_factory=lambda: "tok1")
    record = store.create(key, chat_id="-1001", message_id=9)

    assert record.token == "tok1"
    assert store.by_token("tok1").key == key
    assert store.by_message("-1001", 9).key == key

    store.prune(now=111.0, live_blocked_keys={key})

    assert store.by_token("tok1") is None
    assert store.by_message("-1001", 9) is None


def test_alert_store_replaces_prior_alert_for_same_agent_on_reblock():
    from herdeck.telegram import TelegramAlertStore

    tokens = iter(["tok1", "tok2"])
    key = AgentKey("local", "p1")
    store = TelegramAlertStore(now=lambda: 100.0, token_factory=lambda: next(tokens))

    first = store.create(key, chat_id="-1001", message_id=9)
    second = store.create(key, chat_id="-1001", message_id=10)

    assert first.token == "tok1"
    assert second.token == "tok2"
    assert store.by_token("tok1") is None
    assert store.by_message("-1001", 9) is None
    assert store.by_token("tok2").message_id == 10
    assert store.by_message("-1001", 10).token == "tok2"


def test_alert_store_reserves_callback_token_before_send_and_attaches_message_after():
    from herdeck.telegram import TelegramAlertFormatter, TelegramAlertStore, TelegramBotClient

    key = AgentKey("local", "p1")
    agent = AgentState(key, "codex", "herdeck", Status.BLOCKED)
    store = TelegramAlertStore(now=lambda: 100.0, token_factory=lambda: "tok1")

    record = store.reserve(key)
    text, markup = TelegramAlertFormatter().blocked_alert(
        agent, metadata_body="herdeck · feat/tg", prompt="Approve?", token=record.token
    )

    calls = []
    client = TelegramBotClient(
        "TOK", request=lambda method, fields: calls.append((method, fields)) or {"message_id": 9}
    )
    result = client.send_message(
        chat_id="-1001",
        text=text,
        sound=True,
        message_thread_id=456,
        reply_markup=markup,
    )
    attached = store.attach_message(record.token, chat_id="-1001", message_id=result["message_id"])

    assert record.token == "tok1"
    assert attached is record
    assert store.by_token("tok1").key == key
    assert store.by_message("-1001", 9).token == "tok1"
    assert '"callback_data":"h:tok1:approve"' in calls[0][1]["reply_markup"]
