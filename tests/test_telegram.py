import pytest

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


class FakeControl:
    def __init__(self, prompt="Allow edit?"):
        self.prompt = prompt
        self.read_keys = []

    async def read_prompt(self, key, *, timeout=3.0):
        self.read_keys.append((key, timeout))
        return self.prompt

    def current_agent(self, key):
        return AgentState(key, "codex", "herdeck", Status.BLOCKED)


@pytest.mark.asyncio
async def test_interactor_notify_blocked_sends_prompt_alert_and_stores_mapping():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient(
        "TOK", request=lambda method, fields: calls.append((method, fields)) or {"message_id": 9}
    )
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    control = FakeControl(prompt="Allow edit?\n1. Yes\n2. No")
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
        prompt_max_chars=1200,
    )
    agent = AgentState(AgentKey("local", "p1"), "codex", "herdeck", Status.BLOCKED)

    await interactor.notify_blocked(agent, body="herdeck · main", sound=True, multi_server=False)

    assert control.read_keys == [(AgentKey("local", "p1"), 3.0)]
    method, fields = calls[0]
    assert method == "sendMessage"
    assert fields["chat_id"] == "-1001"
    assert fields["message_thread_id"] == "456"
    assert "Allow edit?" in fields["text"]
    assert "h:tok1:approve" in fields["reply_markup"]
    assert store.by_message("-1001", 9).key == AgentKey("local", "p1")


@pytest.mark.asyncio
async def test_interactor_notify_blocked_sends_safe_alert_when_prompt_read_fails():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    class FailingControl(FakeControl):
        async def read_prompt(self, key, *, timeout=3.0):
            self.read_keys.append((key, timeout))
            raise RuntimeError("read failed")

    calls = []
    client = TelegramBotClient(
        "TOK", request=lambda method, fields: calls.append((method, fields)) or {"message_id": 9}
    )
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    control = FailingControl()
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=None,
        allowed_user_ids=[111],
        store=store,
        prompt_max_chars=1200,
    )
    agent = AgentState(AgentKey("local", "p1"), "codex", "herdeck", Status.BLOCKED)

    await interactor.notify_blocked(agent, body="herdeck · main", sound=True, multi_server=False)

    text = calls[0][1]["text"]
    markup = calls[0][1]["reply_markup"]
    assert "Prompt unavailable; use Read again." in text
    assert "h:tok1:approve" not in markup
    assert "h:tok1:deny" not in markup
    assert "h:tok1:stop" in markup
    assert "h:tok1:read" in markup
    assert store.by_message("-1001", 9).key == AgentKey("local", "p1")


@pytest.mark.asyncio
async def test_interactor_notify_blocked_discards_reserved_alert_when_send_fails():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    def request(method, fields):
        raise RuntimeError("telegram down")

    client = TelegramBotClient("TOK", request=request)
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    control = FakeControl(prompt="Allow edit?")
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=None,
        allowed_user_ids=[111],
        store=store,
        prompt_max_chars=1200,
    )
    agent = AgentState(AgentKey("local", "p1"), "codex", "herdeck", Status.BLOCKED)

    await interactor.notify_blocked(agent, body="herdeck · main", sound=True, multi_server=False)

    assert store.by_token("tok1") is None


@pytest.mark.asyncio
async def test_interactor_notify_blocked_discards_reserved_alert_without_message_id():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    client = TelegramBotClient("TOK", request=lambda method, fields: {})
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    control = FakeControl(prompt="Allow edit?")
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=None,
        allowed_user_ids=[111],
        store=store,
        prompt_max_chars=1200,
    )
    agent = AgentState(AgentKey("local", "p1"), "codex", "herdeck", Status.BLOCKED)

    await interactor.notify_blocked(agent, body="herdeck · main", sound=True, multi_server=False)

    assert store.by_token("tok1") is None


class ActionControl(FakeControl):
    def __init__(self):
        super().__init__()
        self.actions = []

    async def approve(self, key, *, timeout=3.0):
        self.actions.append(("approve", key, timeout))
        from herdeck.app_control import ActionResult

        return ActionResult(True)

    async def deny(self, key, *, timeout=3.0):
        self.actions.append(("deny", key, timeout))
        from herdeck.app_control import ActionResult

        return ActionResult(True)

    async def stop(self, key, *, timeout=3.0):
        self.actions.append(("stop", key, timeout))
        from herdeck.app_control import ActionResult

        return ActionResult(True)


@pytest.mark.asyncio
async def test_callback_approve_requires_allowed_user_chat_and_topic():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient(
        "TOK", request=lambda method, fields: calls.append((method, fields)) or True
    )
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    record = store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)
    control = ActionControl()
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
    )

    ok = await interactor.process_update(
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb1",
                "from": {"id": 111},
                "message": {
                    "chat": {"id": -1001},
                    "message_id": 9,
                    "message_thread_id": 456,
                },
                "data": f"h:{record.token}:approve",
            },
        }
    )

    assert ok is True
    assert control.actions == [("approve", AgentKey("local", "p1"), 3.0)]
    assert ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "sent"}) in calls


@pytest.mark.asyncio
async def test_successful_terminal_callback_discards_token_before_second_tap():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient(
        "TOK", request=lambda method, fields: calls.append((method, fields)) or True
    )
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    record = store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)
    control = ActionControl()
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
    )
    callback = {
        "from": {"id": 111},
        "message": {
            "chat": {"id": -1001},
            "message_id": 9,
            "message_thread_id": 456,
        },
        "data": f"h:{record.token}:approve",
    }

    assert await interactor.process_update(
        {"update_id": 1, "callback_query": {"id": "cb1", **callback}}
    )
    assert store.by_token(record.token) is None
    assert not await interactor.process_update(
        {"update_id": 2, "callback_query": {"id": "cb2", **callback}}
    )

    assert control.actions == [("approve", AgentKey("local", "p1"), 3.0)]
    assert ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "sent"}) in calls
    assert ("answerCallbackQuery", {"callback_query_id": "cb2", "text": "stale"}) in calls


@pytest.mark.asyncio
async def test_callback_from_wrong_user_does_not_run_action():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient(
        "TOK", request=lambda method, fields: calls.append((method, fields)) or True
    )
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    record = store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)
    control = ActionControl()
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
    )

    ok = await interactor.process_update(
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb1",
                "from": {"id": 999},
                "message": {
                    "chat": {"id": -1001},
                    "message_id": 9,
                    "message_thread_id": 456,
                },
                "data": f"h:{record.token}:approve",
            },
        }
    )

    assert ok is False
    assert control.actions == []
    assert ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "not authorized"}) in calls


@pytest.mark.asyncio
async def test_callback_from_wrong_chat_or_topic_does_not_run_action():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    for message in (
        {"chat": {"id": -9999}, "message_id": 9, "message_thread_id": 456},
        {"chat": {"id": -1001}, "message_id": 9, "message_thread_id": 999},
    ):
        calls = []
        client = TelegramBotClient(
            "TOK",
            request=lambda method, fields, calls=calls: calls.append((method, fields))
            or True,
        )
        store = TelegramAlertStore(token_factory=lambda: "tok1")
        record = store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)
        control = ActionControl()
        interactor = TelegramInteractor(
            client,
            control,
            chat_id="-1001",
            message_thread_id=456,
            allowed_user_ids=[111],
            store=store,
        )

        ok = await interactor.process_update(
            {
                "update_id": 1,
                "callback_query": {
                    "id": "cb1",
                    "from": {"id": 111},
                    "message": message,
                    "data": f"h:{record.token}:approve",
                },
            }
        )

        assert ok is False
        assert control.actions == []
        assert (
            "answerCallbackQuery",
            {"callback_query_id": "cb1", "text": "not authorized"},
        ) in calls


@pytest.mark.asyncio
async def test_callback_old_token_is_stale_after_reblock_alert():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    tokens = iter(["tok1", "tok2"])
    client = TelegramBotClient(
        "TOK", request=lambda method, fields: calls.append((method, fields)) or True
    )
    store = TelegramAlertStore(token_factory=lambda: next(tokens))
    key = AgentKey("local", "p1")
    old = store.create(key, chat_id="-1001", message_id=9)
    store.create(key, chat_id="-1001", message_id=10)
    control = ActionControl()
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
    )

    ok = await interactor.process_update(
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb1",
                "from": {"id": 111},
                "message": {
                    "chat": {"id": -1001},
                    "message_id": 9,
                    "message_thread_id": 456,
                },
                "data": f"h:{old.token}:approve",
            },
        }
    )

    assert ok is False
    assert control.actions == []
    assert ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "stale"}) in calls


@pytest.mark.asyncio
async def test_callback_action_error_is_answered_without_raising():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient(
        "TOK", request=lambda method, fields: calls.append((method, fields)) or True
    )
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    record = store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)

    class TimeoutControl(ActionControl):
        async def approve(self, key, *, timeout=3.0):
            self.actions.append(("approve", key, timeout))
            raise TimeoutError("agent did not answer")

    control = TimeoutControl()
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
    )

    ok = await interactor.process_update(
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb1",
                "from": {"id": 111},
                "message": {
                    "chat": {"id": -1001},
                    "message_id": 9,
                    "message_thread_id": 456,
                },
                "data": f"h:{record.token}:approve",
            },
        }
    )

    assert ok is False
    assert control.actions == [("approve", AgentKey("local", "p1"), 3.0)]
    assert ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "timed out"}) in calls


@pytest.mark.asyncio
async def test_callback_failed_action_result_uses_failure_message():
    from herdeck.app_control import ActionResult
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient(
        "TOK", request=lambda method, fields: calls.append((method, fields)) or True
    )
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    record = store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)

    class FailedControl(ActionControl):
        async def approve(self, key, *, timeout=3.0):
            self.actions.append(("approve", key, timeout))
            return ActionResult(False, message="connection lost")

    control = FailedControl()
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
    )

    ok = await interactor.process_update(
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb1",
                "from": {"id": 111},
                "message": {
                    "chat": {"id": -1001},
                    "message_id": 9,
                    "message_thread_id": 456,
                },
                "data": f"h:{record.token}:approve",
            },
        }
    )

    assert ok is False
    assert control.actions == [("approve", AgentKey("local", "p1"), 3.0)]
    assert (
        "answerCallbackQuery",
        {"callback_query_id": "cb1", "text": "connection lost"},
    ) in calls


@pytest.mark.asyncio
async def test_callback_success_discards_alert_so_repeat_tap_is_stale():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient(
        "TOK", request=lambda method, fields: calls.append((method, fields)) or True
    )
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    record = store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)
    control = ActionControl()
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=None,
        allowed_user_ids=[111],
        store=store,
    )
    update = {
        "update_id": 1,
        "callback_query": {
            "id": "cb1",
            "from": {"id": 111},
            "message": {"chat": {"id": -1001}, "message_id": 9},
            "data": f"h:{record.token}:approve",
        },
    }

    assert await interactor.process_update(update) is True
    assert await interactor.process_update(update) is False

    assert control.actions == [("approve", AgentKey("local", "p1"), 3.0)]
    assert store.by_token("tok1") is None
    assert ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "stale"}) in calls


@pytest.mark.asyncio
async def test_poll_once_processes_updates_and_advances_offset():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)

    def request(method, fields):
        calls.append((method, fields))
        if method == "getUpdates":
            return [
                {
                    "update_id": 41,
                    "callback_query": {
                        "id": "cb1",
                        "from": {"id": 111},
                        "message": {"chat": {"id": -1001}, "message_id": 9},
                        "data": "h:tok1:approve",
                    },
                }
            ]
        return True

    control = ActionControl()
    interactor = TelegramInteractor(
        TelegramBotClient("TOK", request=request),
        control,
        chat_id="-1001",
        message_thread_id=None,
        allowed_user_ids=[111],
        store=store,
    )

    await interactor.poll_once(timeout=20)

    assert interactor.offset == 42
    assert control.actions == [("approve", AgentKey("local", "p1"), 3.0)]
    assert calls[0] == (
        "getUpdates",
        {"timeout": "20", "allowed_updates": '["message","callback_query"]'},
    )
    assert ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "sent"}) in calls


@pytest.mark.asyncio
async def test_poll_once_advances_offset_when_callback_ack_fails_after_action():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)

    def request(method, fields):
        calls.append((method, fields))
        if method == "getUpdates":
            return [
                {
                    "update_id": 41,
                    "callback_query": {
                        "id": "cb1",
                        "from": {"id": 111},
                        "message": {"chat": {"id": -1001}, "message_id": 9},
                        "data": "h:tok1:approve",
                    },
                }
            ]
        if method == "answerCallbackQuery":
            raise RuntimeError("telegram ack failed")
        return True

    control = ActionControl()
    interactor = TelegramInteractor(
        TelegramBotClient("TOK", request=request),
        control,
        chat_id="-1001",
        message_thread_id=None,
        allowed_user_ids=[111],
        store=store,
    )

    with pytest.raises(RuntimeError, match="telegram ack failed"):
        await interactor.poll_once(timeout=20)

    assert interactor.offset == 42
    assert control.actions == [("approve", AgentKey("local", "p1"), 3.0)]


@pytest.mark.asyncio
async def test_poll_once_skips_updates_when_generation_guard_is_stale():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)

    def request(method, fields):
        calls.append((method, fields))
        if method == "getUpdates":
            return [
                {
                    "update_id": 41,
                    "callback_query": {
                        "id": "cb1",
                        "from": {"id": 111},
                        "message": {"chat": {"id": -1001}, "message_id": 9},
                        "data": "h:tok1:approve",
                    },
                }
            ]
        return True

    control = ActionControl()
    interactor = TelegramInteractor(
        TelegramBotClient("TOK", request=request),
        control,
        chat_id="-1001",
        message_thread_id=None,
        allowed_user_ids=[111],
        store=store,
    )

    await interactor.poll_once(timeout=20, is_current=lambda: False)

    assert interactor.offset is None
    assert control.actions == []
    assert [method for method, _ in calls] == ["getUpdates"]


@pytest.mark.asyncio
async def test_poll_once_disables_inbound_on_webhook_conflict_but_keeps_outbound_alerts():
    from herdeck.telegram import (
        TelegramAlertStore,
        TelegramApiError,
        TelegramBotClient,
        TelegramInteractor,
    )

    calls = []

    def request(method, fields):
        calls.append((method, fields))
        if method == "getUpdates":
            raise TelegramApiError(
                409,
                "Conflict: can't use getUpdates method while webhook is active",
            )
        return {"message_id": 9}

    interactor = TelegramInteractor(
        TelegramBotClient("TOK", request=request),
        FakeControl(prompt="Allow edit?"),
        chat_id="-1001",
        message_thread_id=None,
        allowed_user_ids=[111],
        store=TelegramAlertStore(token_factory=lambda: "tok1"),
    )

    await interactor.poll_once(timeout=20)
    await interactor.poll_once(timeout=20)
    await interactor.notify_blocked(
        AgentState(AgentKey("local", "p1"), "codex", "herdeck", Status.BLOCKED),
        body="herdeck · main",
        sound=True,
        multi_server=False,
    )

    assert interactor.inbound_disabled is True
    assert [method for method, _ in calls] == ["getUpdates", "sendMessage"]


class ReplyControl(ActionControl):
    async def send_text(self, key, text, *, timeout=3.0):
        self.actions.append(("send_text", key, text, timeout))
        from herdeck.app_control import ActionResult

        return ActionResult(True)


@pytest.mark.asyncio
async def test_reply_to_known_alert_sends_text_to_agent():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient(
        "TOK",
        request=lambda method, fields: calls.append((method, fields)) or {"message_id": 10},
    )
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)
    control = ReplyControl()
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
    )

    ok = await interactor.process_update(
        {
            "update_id": 2,
            "message": {
                "chat": {"id": -1001},
                "from": {"id": 111},
                "message_thread_id": 456,
                "reply_to_message": {"message_id": 9},
                "text": "please continue",
            },
        }
    )

    assert ok is True
    assert control.actions == [
        ("send_text", AgentKey("local", "p1"), "please continue", 3.0)
    ]
    assert calls[-1][0] == "sendMessage"
    assert "sent to local:p1" in calls[-1][1]["text"]


@pytest.mark.asyncio
async def test_reply_send_text_timeout_reports_failure_status():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient(
        "TOK",
        request=lambda method, fields: calls.append((method, fields)) or {"message_id": 10},
    )
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)

    class TimeoutReplyControl(ReplyControl):
        async def send_text(self, key, text, *, timeout=3.0):
            self.actions.append(("send_text", key, text, timeout))
            raise TimeoutError("agent did not answer")

    control = TimeoutReplyControl()
    interactor = TelegramInteractor(
        client,
        control,
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
    )

    ok = await interactor.process_update(
        {
            "update_id": 2,
            "message": {
                "chat": {"id": -1001},
                "from": {"id": 111},
                "message_thread_id": 456,
                "reply_to_message": {"message_id": 9},
                "message_id": 30,
                "text": "please continue",
            },
        }
    )

    assert ok is False
    assert control.actions == [
        ("send_text", AgentKey("local", "p1"), "please continue", 3.0)
    ]
    assert calls[-1] == (
        "sendMessage",
        {
            "chat_id": "-1001",
            "text": "delivery timed out",
            "disable_notification": "true",
            "message_thread_id": "456",
            "reply_parameters": '{"message_id":30}',
        },
    )


@pytest.mark.asyncio
async def test_reply_from_wrong_chat_or_topic_does_not_send_text():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    for message in (
        {
            "chat": {"id": -9999},
            "from": {"id": 111},
            "message_thread_id": 456,
            "reply_to_message": {"message_id": 9},
            "message_id": 30,
            "text": "please continue",
        },
        {
            "chat": {"id": -1001},
            "from": {"id": 111},
            "message_thread_id": 999,
            "reply_to_message": {"message_id": 9},
            "message_id": 30,
            "text": "please continue",
        },
    ):
        calls = []
        client = TelegramBotClient(
            "TOK",
            request=lambda method, fields, calls=calls: calls.append((method, fields))
            or {"message_id": 10},
        )
        store = TelegramAlertStore(token_factory=lambda: "tok1")
        store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)
        control = ReplyControl()
        interactor = TelegramInteractor(
            client,
            control,
            chat_id="-1001",
            message_thread_id=456,
            allowed_user_ids=[111],
            store=store,
        )

        ok = await interactor.process_update({"update_id": 2, "message": message})

        assert ok is False
        assert control.actions == []
        assert calls == []


@pytest.mark.asyncio
async def test_status_requires_authorized_user_chat_and_topic():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    for message in (
        {"chat": {"id": -1001}, "from": {"id": 999}, "message_thread_id": 456},
        {"chat": {"id": -9999}, "from": {"id": 111}, "message_thread_id": 456},
        {"chat": {"id": -1001}, "from": {"id": 111}, "message_thread_id": 999},
    ):
        calls = []
        client = TelegramBotClient(
            "TOK",
            request=lambda method, fields, calls=calls: calls.append((method, fields))
            or {"message_id": 10},
        )
        store = TelegramAlertStore(token_factory=lambda: "tok1")
        store.create(AgentKey("local", "p1"), chat_id="-1001", message_id=9)
        interactor = TelegramInteractor(
            client,
            ReplyControl(),
            chat_id="-1001",
            message_thread_id=456,
            allowed_user_ids=[111],
            store=store,
        )
        message = {**message, "text": "/status"}

        ok = await interactor.process_update({"update_id": 3, "message": message})

        assert ok is False
        assert calls == []


@pytest.mark.asyncio
async def test_status_prunes_no_longer_blocked_alerts_before_listing():
    from herdeck.telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

    calls = []
    client = TelegramBotClient(
        "TOK",
        request=lambda method, fields: calls.append((method, fields)) or {"message_id": 10},
    )
    store = TelegramAlertStore(token_factory=lambda: "tok1")
    key = AgentKey("local", "p1")
    store.create(key, chat_id="-1001", message_id=9)

    class DoneControl(ReplyControl):
        def current_agent(self, key):
            return AgentState(key, "codex", "herdeck", Status.DONE)

    interactor = TelegramInteractor(
        client,
        DoneControl(),
        chat_id="-1001",
        message_thread_id=456,
        allowed_user_ids=[111],
        store=store,
    )

    ok = await interactor.process_update(
        {
            "update_id": 3,
            "message": {
                "chat": {"id": -1001},
                "from": {"id": 111},
                "message_thread_id": 456,
                "text": "/status",
                "message_id": 30,
            },
        }
    )

    assert ok is True
    assert store.by_token("tok1") is None
    assert (
        "sendMessage",
        {
            "chat_id": "-1001",
            "text": "no tracked blocked alerts",
            "disable_notification": "true",
            "message_thread_id": "456",
            "reply_parameters": '{"message_id":30}',
        },
    ) in calls
