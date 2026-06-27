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


class TelegramInteractor:
    def __init__(
        self,
        client: TelegramBotClient,
        control,
        *,
        chat_id: str,
        message_thread_id: int | None,
        allowed_user_ids: list[int],
        store: TelegramAlertStore | None = None,
        prompt_max_chars: int = 1200,
    ):
        self._client = client
        self._control = control
        self._chat_id = str(chat_id)
        self._message_thread_id = message_thread_id
        self._allowed_user_ids = set(int(v) for v in allowed_user_ids)
        self._store = store or TelegramAlertStore()
        self._formatter = TelegramAlertFormatter(prompt_max_chars=prompt_max_chars)
        self._inbound_disabled = False
        self._offset: int | None = None

    @property
    def offset(self) -> int | None:
        return self._offset

    @property
    def inbound_disabled(self) -> bool:
        return self._inbound_disabled

    async def notify_blocked(
        self, agent: AgentState, *, body: str, sound: bool, multi_server: bool
    ) -> None:
        record = self._store.reserve(agent.key)
        try:
            prompt = await self._control.read_prompt(agent.key, timeout=3.0)
        except Exception:
            prompt = ""
        text, markup = self._formatter.blocked_alert(
            agent,
            metadata_body=body,
            prompt=str(prompt or ""),
            token=record.token,
        )
        try:
            result = await asyncio.to_thread(
                self._client.send_message,
                chat_id=self._chat_id,
                text=text,
                sound=sound,
                message_thread_id=self._message_thread_id,
                reply_markup=markup,
            )
        except Exception:
            self._store.discard(record.token)
            log.debug("telegram blocked alert send failed", exc_info=True)
            return
        try:
            message_id = int((result or {}).get("message_id", -1))
        except (TypeError, ValueError):
            message_id = -1
        if message_id <= 0:
            self._store.discard(record.token)
            return
        self._store.attach_message(record.token, chat_id=self._chat_id, message_id=message_id)

    def _is_webhook_conflict(self, exc: TelegramApiError) -> bool:
        return exc.error_code == 409 and "webhook" in exc.description.lower()

    def _live_blocked_keys(self) -> set[AgentKey]:
        live = set()
        for record in self._store.records():
            try:
                agent = self._control.current_agent(record.key)
            except Exception:
                continue
            if agent is not None and agent.status is Status.BLOCKED:
                live.add(record.key)
        return live

    def _prune_alerts(self) -> None:
        self._store.prune(live_blocked_keys=self._live_blocked_keys())

    async def process_update(self, update: dict) -> bool:
        self._prune_alerts()
        if "callback_query" in update:
            return await self._process_callback(update["callback_query"])
        if "message" in update:
            return await self._process_message(update["message"])
        return False

    async def _process_callback(self, query: dict) -> bool:
        cb_id = str(query.get("id", ""))
        message = query.get("message", {})
        if not self._authorized(query.get("from", {}), message):
            await asyncio.to_thread(
                self._client.answer_callback_query, cb_id, text="not authorized"
            )
            return False
        data = str(query.get("data", ""))
        parts = data.split(":")
        if len(parts) != 3 or parts[0] != "h":
            await asyncio.to_thread(self._client.answer_callback_query, cb_id, text="stale")
            return False
        record = self._store.by_token(parts[1])
        if record is None or not self._callback_matches_record(record, message):
            await asyncio.to_thread(self._client.answer_callback_query, cb_id, text="stale")
            return False
        action = parts[2]
        try:
            if action == "approve":
                result = await self._control.approve(record.key, timeout=3.0)
                status, ok = self._action_status(result)
            elif action == "deny":
                result = await self._control.deny(record.key, timeout=3.0)
                status, ok = self._action_status(result)
            elif action == "stop":
                result = await self._control.stop(record.key, timeout=3.0)
                status, ok = self._action_status(result)
            elif action == "read":
                await self._refresh_prompt(record)
                status = "refreshed"
                ok = True
            else:
                await asyncio.to_thread(self._client.answer_callback_query, cb_id, text="stale")
                return False
        except TimeoutError:
            await asyncio.to_thread(self._client.answer_callback_query, cb_id, text="timed out")
            return False
        except Exception:
            await asyncio.to_thread(self._client.answer_callback_query, cb_id, text="failed")
            return False
        if ok and action != "read":
            self._store.discard(record.token)
        await asyncio.to_thread(self._client.answer_callback_query, cb_id, text=status)
        return ok

    def _callback_matches_record(self, record: TelegramAlertRecord, message: dict) -> bool:
        try:
            message_id = int(message.get("message_id", -1))
        except (TypeError, ValueError):
            return False
        return record.chat_id == self._chat_id and record.message_id == message_id

    def _authorized(self, user: dict, message: dict) -> bool:
        try:
            user_id = int(user.get("id", 0))
        except (TypeError, ValueError):
            return False
        if user_id not in self._allowed_user_ids:
            return False
        chat = message.get("chat", {})
        if str(chat.get("id")) != self._chat_id:
            return False
        if self._message_thread_id is not None:
            try:
                thread_id = int(message.get("message_thread_id", 0))
            except (TypeError, ValueError):
                return False
            return thread_id == self._message_thread_id
        return True

    def _action_status(self, result) -> tuple[str, bool]:
        if result.sent:
            return "sent", True
        if result.skipped:
            return "skipped", True
        return result.message or "failed", False

    async def _refresh_prompt(self, record: TelegramAlertRecord) -> None:
        agent = self._control.current_agent(record.key)
        if agent is None:
            text = "agent is no longer available"
            markup = None
        else:
            try:
                prompt = await self._control.read_prompt(record.key, timeout=3.0)
            except Exception:
                prompt = ""
            text, markup = self._formatter.blocked_alert(
                agent,
                metadata_body=f"{agent.repo or agent.label}",
                prompt=str(prompt or ""),
                token=record.token,
            )
        await asyncio.to_thread(
            self._client.edit_message_text,
            chat_id=record.chat_id,
            message_id=record.message_id,
            text=text,
            message_thread_id=self._message_thread_id,
            reply_markup=markup,
        )

    async def _process_message(self, message: dict) -> bool:
        if not self._authorized(message.get("from", {}), message):
            return False
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return False
        if text.strip() == "/status":
            await self._send_status(message)
            return True
        reply = message.get("reply_to_message") or {}
        reply_id = reply.get("message_id")
        if reply_id is None:
            return False
        try:
            record = self._store.by_message(self._chat_id, int(reply_id))
        except (TypeError, ValueError):
            record = None
        if record is None:
            await self._send_thread_message(
                "alert is stale",
                reply_to_message_id=message.get("message_id"),
            )
            return False
        try:
            result = await self._control.send_text(record.key, text, timeout=3.0)
            status = (
                f"sent to {record.key.server_id}:{record.key.pane_id}"
                if result.sent
                else result.message or "delivery failed"
            )
            ok = result.sent
        except TimeoutError:
            status = "delivery timed out"
            ok = False
        except Exception:
            status = "delivery failed"
            ok = False
        await self._send_thread_message(status, reply_to_message_id=message.get("message_id"))
        return ok

    async def _send_status(self, message: dict) -> None:
        self._prune_alerts()
        records = self._store.records()
        if records:
            lines = ["tracked blocked alerts:"]
            lines.extend(f"- {r.key.server_id}:{r.key.pane_id}" for r in records)
        else:
            lines = ["no tracked blocked alerts"]
        await self._send_thread_message(
            "\n".join(lines),
            reply_to_message_id=message.get("message_id"),
        )

    async def _send_thread_message(
        self, text: str, *, reply_to_message_id: int | None = None
    ) -> None:
        await asyncio.to_thread(
            self._client.send_message,
            chat_id=self._chat_id,
            text=text,
            sound=False,
            message_thread_id=self._message_thread_id,
            reply_to_message_id=reply_to_message_id,
        )

    async def poll_once(
        self, *, timeout: int = 20, is_current: Callable[[], bool] | None = None
    ) -> None:
        if self._inbound_disabled:
            return
        try:
            updates = await asyncio.to_thread(
                self._client.get_updates,
                offset=self.offset,
                timeout=timeout,
            )
        except TelegramApiError as exc:
            if self._is_webhook_conflict(exc):
                self._inbound_disabled = True
                log.warning("telegram inbound polling disabled: %s", exc.description)
                return
            raise
        updates = updates or []
        if is_current is not None and not is_current():
            return
        for update in updates:
            if is_current is not None and not is_current():
                return
            update_id = update.get("update_id") if isinstance(update, dict) else None
            try:
                if isinstance(update, dict):
                    await self.process_update(update)
            finally:
                if isinstance(update_id, int):
                    self._offset = update_id + 1
