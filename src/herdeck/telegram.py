from __future__ import annotations

import asyncio  # noqa: F401 - reserved for interactive Telegram poller wiring in this module.
import json
import logging
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from .model import AgentKey, AgentState, Status

log = logging.getLogger("herdeck.telegram")


class TelegramApiError(RuntimeError):
    def __init__(self, error_code: int, description: str):
        super().__init__(description)
        self.error_code = error_code
        self.description = description


def _request_json(token: str, method: str, fields: dict[str, str]):
    data = urllib.parse.urlencode(fields).encode()
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        with urllib.request.urlopen(url, data=data, timeout=25) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode() or "{}")
        except json.JSONDecodeError:
            payload = {}
        raise TelegramApiError(
            int(payload.get("error_code", exc.code)),
            str(payload.get("description", exc)),
        ) from exc
    if not payload.get("ok"):
        raise TelegramApiError(
            int(payload.get("error_code", 0)),
            str(payload.get("description", f"Telegram {method} failed")),
        )
    return payload.get("result")


class TelegramBotClient:
    def __init__(
        self, token: str, *, request: Callable[[str, dict[str, str]], object] | None = None
    ):
        self._token = token
        self._request = request or (lambda method, fields: _request_json(token, method, fields))

    def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        sound: bool,
        message_thread_id: int | None = None,
        reply_markup: dict | None = None,
        reply_to_message_id: int | None = None,
    ):
        fields = {
            "chat_id": str(chat_id),
            "text": text,
            "disable_notification": "false" if sound else "true",
        }
        if message_thread_id is not None:
            fields["message_thread_id"] = str(message_thread_id)
        if reply_markup is not None:
            fields["reply_markup"] = json.dumps(reply_markup, separators=(",", ":"))
        if reply_to_message_id is not None:
            fields["reply_parameters"] = json.dumps(
                {"message_id": int(reply_to_message_id)}, separators=(",", ":")
            )
        return self._request("sendMessage", fields)

    def get_updates(self, *, offset: int | None, timeout: int = 20):
        fields = {
            "timeout": str(timeout),
            "allowed_updates": json.dumps(["message", "callback_query"], separators=(",", ":")),
        }
        if offset is not None:
            fields["offset"] = str(offset)
        return self._request("getUpdates", fields)

    def answer_callback_query(self, callback_query_id: str, *, text: str = ""):
        fields = {"callback_query_id": callback_query_id}
        if text:
            fields["text"] = text
        return self._request("answerCallbackQuery", fields)

    def edit_message_text(
        self,
        *,
        chat_id: str,
        message_id: int,
        text: str,
        message_thread_id: int | None = None,
        reply_markup: dict | None = None,
    ):
        fields = {"chat_id": str(chat_id), "message_id": str(message_id), "text": text}
        if reply_markup is not None:
            fields["reply_markup"] = json.dumps(reply_markup, separators=(",", ":"))
        return self._request("editMessageText", fields)


class TelegramAlertFormatter:
    def __init__(self, *, prompt_max_chars: int = 1200):
        self._prompt_max_chars = prompt_max_chars

    def blocked_alert(
        self, agent: AgentState, *, metadata_body: str, prompt: str, token: str
    ) -> tuple[str, dict]:
        has_prompt = bool(prompt.strip())
        prompt = self._truncate(prompt.strip()) if has_prompt else "Prompt unavailable; use Read again."
        text = (
            f"{agent.agent_type} {Status.BLOCKED.value}\n"
            f"{metadata_body} · {agent.key.server_id}:{agent.key.pane_id}\n\n"
            f"Waiting for:\n{prompt}\n\n"
            "Reply to this message to send text to the agent."
        )
        if has_prompt:
            keyboard = [
                [
                    {"text": "Approve", "callback_data": f"h:{token}:approve"},
                    {"text": "Deny", "callback_data": f"h:{token}:deny"},
                    {"text": "Stop", "callback_data": f"h:{token}:stop"},
                ],
                [{"text": "Read again", "callback_data": f"h:{token}:read"}],
            ]
        else:
            keyboard = [
                [{"text": "Stop", "callback_data": f"h:{token}:stop"}],
                [{"text": "Read again", "callback_data": f"h:{token}:read"}],
            ]
        markup = {"inline_keyboard": keyboard}
        return text, markup

    def _truncate(self, text: str) -> str:
        if len(text) <= self._prompt_max_chars:
            return text
        return text[: self._prompt_max_chars] + "..."


@dataclass
class TelegramAlertRecord:
    token: str
    key: AgentKey
    chat_id: str
    message_id: int
    created_at: float


class TelegramAlertStore:
    def __init__(
        self,
        *,
        now: Callable[[], float] = time.time,
        ttl_seconds: float = 24 * 60 * 60,
        token_factory: Callable[[], str] | None = None,
    ):
        self._now = now
        self._ttl_seconds = ttl_seconds
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(8))
        self._by_token: dict[str, TelegramAlertRecord] = {}
        self._by_message: dict[tuple[str, int], TelegramAlertRecord] = {}

    def create(self, key: AgentKey, *, chat_id: str, message_id: int) -> TelegramAlertRecord:
        record = self.reserve(key)
        return self.attach_message(record.token, chat_id=chat_id, message_id=message_id)

    def reserve(self, key: AgentKey) -> TelegramAlertRecord:
        for old in [record for record in self._by_token.values() if record.key == key]:
            self.discard(old.token)
        token = self._token_factory()
        record = TelegramAlertRecord(token, key, "", -1, self._now())
        self._by_token[token] = record
        return record

    def attach_message(
        self, token: str, *, chat_id: str, message_id: int
    ) -> TelegramAlertRecord:
        record = self._by_token[token]
        self._by_message.pop((record.chat_id, record.message_id), None)
        record.chat_id = str(chat_id)
        record.message_id = int(message_id)
        self._by_message[(record.chat_id, record.message_id)] = record
        return record

    def by_token(self, token: str) -> TelegramAlertRecord | None:
        return self._by_token.get(token)

    def by_message(self, chat_id: str, message_id: int) -> TelegramAlertRecord | None:
        return self._by_message.get((str(chat_id), int(message_id)))

    def discard(self, token: str) -> None:
        record = self._by_token.pop(token, None)
        if record is not None:
            self._by_message.pop((record.chat_id, record.message_id), None)

    def prune(
        self, *, now: float | None = None, live_blocked_keys: set[AgentKey] | None = None
    ) -> None:
        current = self._now() if now is None else now
        live = live_blocked_keys
        for token, record in list(self._by_token.items()):
            expired = current - record.created_at > self._ttl_seconds
            not_live = live is not None and record.key not in live
            if expired or not_live:
                self.discard(token)

    def records(self) -> list[TelegramAlertRecord]:
        return list(self._by_token.values())
