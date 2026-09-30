"""Bridge-side Telegram alerts: the agent control (this part) and the notifier.

``BridgeAgentControl`` implements the control protocol ``TelegramInteractor``
calls. All methods are coroutines (or plain sync for ``current_agent`` /
``reset_confirmation``) running on the bridge's own event loop, exactly as the
interactor awaits them, so no thread bridging is needed. Every answer goes
through the injected ``execute`` (in production a partial of
``bridge_answers.execute_answer``) and therefore through the same episode guard
as a deck's answer.

``BridgeNotifier`` decides and sends the Telegram alerts of this bridge from
its own lifecycle events (``EventHub.add_listener``), wire snapshots, presence
and shared settings, so alerts keep going out while every client Mac sleeps.
Its decision rules are the runtime's (deckapp/live.py + live_events.py):
the same ``RunTracker`` / ``SubagentBursts`` / ``NotifyThrottle``,
quiet-done deferral, reminders x1 x2 x3, answered-then-done suppression.
All Bot API calls run in ``asyncio.to_thread`` on a client whose errors are
scrubbed of the bot token before anything (status, logs, results) sees them.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import SimpleNamespace

from .app_control import ActionResult
from .bridge_telegram import BridgeTelegramStore, TelegramSettings
from .commands import build_action_command, command_to_msg, profile_for
from .config import ConfigError
from .events import EventHub
from .i18n import tr
from .layout import prompt_excerpt
from .model import AgentKey, AgentState, Status
from .notify import NotifyThrottle, _warn_failure, event_title
from .notify_events import RunTracker, SubagentBursts, event_notification_body
from .presence_hub import PresenceHub
from .protocol import _pane_to_state
from .shared_settings import SharedSettings, parse_shared
from .telegram import TelegramAlertStore, TelegramApiError, TelegramBotClient, TelegramInteractor

log = logging.getLogger(__name__)

BY = "telegram"
# Same arming window as RuntimeAgentControl: a Stop tap from long ago can
# never be completed by a later single tap.
CONFIRM_TTL_S = 60.0


def shared_settings_of(store) -> SharedSettings:
    """The bridge's shared settings; built-in defaults when unset/invalid."""
    raw = store.raw
    if raw is not None:
        try:
            return parse_shared(raw)
        except ConfigError as exc:
            log.warning("shared settings invalid, using defaults: %s", exc)
    return parse_shared({})


class BridgeAgentControl:
    def __init__(
        self,
        *,
        execute: Callable[[dict, str], Awaitable[dict]],
        agents: Callable[[], dict[AgentKey, AgentState]],
        episodes: EventHub,
        settings,
        read_prompt: Callable[[str], Awaitable[str | None]],
        clock: Callable[[], float] = time.monotonic,
    ):
        self._execute = execute
        self._agents = agents
        self._episodes = episodes
        self._settings = settings
        self._read_prompt = read_prompt
        self._clock = clock
        self._pending_confirm: tuple[str, AgentKey] | None = None
        self._pending_confirm_at = 0.0

    # --- state --------------------------------------------------------------
    def _shared(self) -> SharedSettings:
        return shared_settings_of(self._settings)

    def current_agent(self, key: AgentKey) -> AgentState | None:
        return self._agents().get(key)

    def reset_confirmation(self, key: AgentKey | None = None) -> None:
        if key is None or (self._pending_confirm is not None and self._pending_confirm[1] == key):
            self._pending_confirm = None

    def _episode_fields(self, agent: AgentState) -> dict:
        """episode_id / prompt_revision of the pane's open blocked episode."""
        ep = self._episodes.open_episode(agent.key.pane_id)
        if ep is None or ep.kind != "blocked":
            return {}
        if agent.terminal_id and ep.terminal_id not in ("", agent.terminal_id):
            return {}
        fields = {"episode_id": ep.id}
        if ep.revision:
            fields["prompt_revision"] = ep.revision
        return fields

    # --- reads --------------------------------------------------------------
    async def read_prompt(self, key: AgentKey, *, timeout: float | None = 3.0) -> str:
        agent = self.current_agent(key)
        if agent is None:
            return ""
        ep = self._episodes.open_episode(key.pane_id)
        if ep is not None and ep.kind == "blocked" and ep.prompt:
            return ep.prompt
        return (await self._read_prompt(key.pane_id)) or ""

    # --- answers ------------------------------------------------------------
    async def approve(
        self, key: AgentKey, *, timeout: float | None = 3.0, force: bool = False,
        always: bool = False, confirmed: bool = False,
    ) -> ActionResult:
        return await self._act("approve", key, force=force, always=always, confirmed=confirmed)

    async def deny(
        self, key: AgentKey, *, timeout: float | None = 3.0, force: bool = False,
        confirmed: bool = False,
    ) -> ActionResult:
        return await self._act("deny", key, force=force, always=False, confirmed=confirmed)

    async def stop(
        self, key: AgentKey, *, timeout: float | None = 3.0, confirmed: bool = False
    ) -> ActionResult:
        return await self._act("stop", key, force=True, always=False, confirmed=confirmed)

    async def send_text(
        self, key: AgentKey, text: str, *, timeout: float | None = 3.0
    ) -> ActionResult:
        agent = self.current_agent(key)
        if agent is None:
            return ActionResult(False, message="agent is no longer available")
        if agent.backend != "herdr":
            return ActionResult(False, message="agent is not answerable from the bridge")
        msg: dict = {"type": "send_text", "req": "tg", "pane_id": key.pane_id, "text": text}
        if agent.terminal_id:
            msg["terminal_id"] = agent.terminal_id
        msg.update(self._episode_fields(agent))
        return await self._run(msg)

    async def _act(
        self, action: str, key: AgentKey, *, force: bool, always: bool, confirmed: bool
    ) -> ActionResult:
        agent = self.current_agent(key)
        if agent is None:
            return ActionResult(False, message="agent is no longer available")
        if agent.backend != "herdr":
            return ActionResult(False, message="agent is not answerable from the bridge")
        shared = self._shared()
        action_id = self._action_id(action, force=force, always=always)
        if action_id in shared.safety.require_confirm_for and not confirmed:
            armed = (
                self._pending_confirm == (action_id, key)
                and self._clock() - self._pending_confirm_at <= CONFIRM_TTL_S
            )
            if not armed:
                self._pending_confirm = (action_id, key)
                self._pending_confirm_at = self._clock()
                return ActionResult(False, message="confirmation required")
        self._pending_confirm = None
        # profile_for only reads ``.profiles``.
        profile = profile_for(SimpleNamespace(profiles=shared.answer_profiles), agent.agent_type)
        command = build_action_command(action, agent, profile, force=force, always=always)
        msg = command_to_msg(command, "tg")
        msg.update(self._episode_fields(agent))
        return await self._run(msg)

    @staticmethod
    def _action_id(action: str, *, force: bool, always: bool) -> str:
        if action == "stop" or force:
            return "act_force"
        if action == "approve" and always:
            return "approve_always"
        return action

    async def _run(self, msg: dict) -> ActionResult:
        try:
            data = await self._execute(msg, BY)
        except Exception as exc:  # never let an answer crash the interactor
            log.warning("bridge answer failed: %s", type(exc).__name__)
            return ActionResult(False, message="answer failed")
        if "error" in data:
            return ActionResult(False, message=str(data["error"]))
        return ActionResult(
            data.get("sent") is True,
            skipped=data.get("skipped") is True,
            message=str(data.get("message") or ""),
        )


# --- BridgeNotifier -------------------------------------------------------------

# Same as deckapp.live.REMIND_MAX (a test pins the two together).
REMIND_MAX = 3
# getUpdates long-poll length (the runtime's TELEGRAM_POLL_TIMEOUT_S).
POLL_TIMEOUT_S = 20
# 409 (a webhook, or another getUpdates poller on the same bot): inbound stays
# off this long, then polling is tried again.
INBOUND_RETRY_S = 60.0
# Any other poll failure (network, 429, a malformed update) backs off this long.
POLL_ERROR_BACKOFF_S = 5.0
# No token: how often the poller looks again (``refresh()`` wakes it at once).
IDLE_WAIT_S = 5.0
RECENT_CHATS_MAX = 10
LAST_ERROR_MAX = 300
_KNOWN_EPISODES_MAX = 1024
_ANSWERED_EPISODES_MAX = 256
# Anything shaped like a bot token (the current one, an old one, the secret
# half alone is covered by the explicit replace in ``scrub``).
_TOKEN_SHAPE_RE = re.compile(r"[0-9]{5,16}:[A-Za-z0-9_-]{30,64}")
_LABEL_UNSAFE_RE = re.compile("[\x00-\x1f\x7f-\x9f‪-‮⁦-⁩]")
_CHAT_TYPES = frozenset({"private", "group", "supergroup", "channel"})
REDACTED = "<redacted>"


def scrub(text: str, token: str | None = None) -> str:
    """``text`` with the bot token (and anything token-shaped) removed.
    The Bot API URL is ``.../bot<token>/method``; some errors echo it."""
    if token:
        text = text.replace(token, REDACTED)
        secret = token.partition(":")[2]
        if len(secret) >= 16:
            text = text.replace(secret, REDACTED)
    return _TOKEN_SHAPE_RE.sub(REDACTED, text)


class TelegramSendError(RuntimeError):
    """A non-API Bot API failure (network, timeout), token-scrubbed."""


class _GuardedClient:
    """A ``TelegramBotClient`` whose failures are re-raised token-scrubbed
    (the original exception, which may carry the URL, is dropped: ``from
    None``) and reported to the notifier, together with every result.
    Runs on ``asyncio.to_thread`` worker threads; ``report`` must be safe
    to call from any thread."""

    def __init__(self, client, token: str, report: Callable[[str, object, str | None], None]):
        self._client = client
        self._token = token
        self._report = report

    def _call(self, method: str, api: str, *args, **kwargs):
        try:
            result = getattr(self._client, method)(*args, **kwargs)
        except Exception as exc:
            text = scrub(str(exc) or type(exc).__name__, self._token)
            self._report(api, None, text)
            if isinstance(exc, TelegramApiError):
                raise TelegramApiError(exc.error_code, text) from None
            raise TelegramSendError(f"{type(exc).__name__}: {text}") from None
        self._report(api, result, None)
        return result

    def send_message(self, **kwargs):
        return self._call("send_message", "sendMessage", **kwargs)

    def get_updates(self, **kwargs):
        return self._call("get_updates", "getUpdates", **kwargs)

    def answer_callback_query(self, *args, **kwargs):
        return self._call("answer_callback_query", "answerCallbackQuery", *args, **kwargs)

    def edit_message_text(self, **kwargs):
        return self._call("edit_message_text", "editMessageText", **kwargs)


@dataclass
class _Reminder:
    episode: str
    since: float  # wall seconds the block began
    sent: int
    handle: object | None = None


def _in_episode(state: AgentState | None, status: Status, episode_id: str) -> bool:
    return (
        state is not None
        and state.status is status
        and (not state.episode_id or state.episode_id == episode_id)
    )


def _label(value: object, limit: int = 128) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(_LABEL_UNSAFE_RE.sub(" ", value).split())[:limit]


def _chat_entry(update: object) -> dict | None:
    """``{chat_id, title, type, message_thread_id, topic_name}`` of the chat
    an update came from, or None (anything malformed is ignored)."""
    if not isinstance(update, dict):
        return None
    msg = update.get("message")
    if not isinstance(msg, dict):
        query = update.get("callback_query")
        msg = query.get("message") if isinstance(query, dict) else None
    if not isinstance(msg, dict):
        return None
    chat = msg.get("chat")
    if not isinstance(chat, dict) or type(chat.get("id")) is not int:
        return None
    title = _label(chat.get("title")) or _label(chat.get("username")) or _label(
        " ".join(str(chat.get(k) or "") for k in ("first_name", "last_name"))
    )
    kind = chat.get("type") if chat.get("type") in _CHAT_TYPES else ""
    thread = msg.get("message_thread_id")
    if msg.get("is_topic_message") is not True or type(thread) is not int or thread < 1:
        thread = None
    topic = ""
    if thread is not None:
        for holder in (msg, msg.get("reply_to_message")):
            created = holder.get("forum_topic_created") if isinstance(holder, dict) else None
            if isinstance(created, dict) and _label(created.get("name")):
                topic = _label(created.get("name"))
                break
    return {
        "chat_id": str(chat["id"]),
        "title": title,
        "type": kind,
        "message_thread_id": thread,
        "topic_name": topic,
    }


def _default_timer(delay: float, fn):
    return asyncio.get_running_loop().call_later(max(0.0, delay), fn)


class BridgeNotifier:
    """Telegram alerts sent by the bridge (one-way and interactive).

    Event-loop only. ``observe_panes`` gets every full wire snapshot,
    ``on_event`` every lifecycle event frame of the bridge's ``EventHub``;
    both are synchronous and never raise. ``run()`` long-polls the Bot API
    (interactive answers, or chat discovery); ``close()`` stops it all.
    """

    def __init__(
        self,
        *,
        server_id: str,
        telegram: BridgeTelegramStore,
        settings,
        presence: PresenceHub | None,
        control,
        client_factory=TelegramBotClient,
        clock: Callable[[], float] = time.time,
        loop_timer=None,
        poll_timeout: int = POLL_TIMEOUT_S,
        sleep=None,
    ):
        self._server_id = server_id
        self._tg = telegram
        self._settings = settings
        self._presence = presence
        self._control = control
        self._client_factory = client_factory
        self._clock = clock
        self._timer = loop_timer or _default_timer
        self._poll_timeout = poll_timeout
        self._sleep = sleep or self._wait
        self.on_status_change: Callable[[], None] | None = None
        # decisions
        self._agents: dict[AgentKey, AgentState] = {}
        self._observed = False
        self._runs = RunTracker()
        self._bursts = SubagentBursts()
        self._throttle = NotifyThrottle(clock=clock)
        self._pending_done: dict[AgentKey, tuple[int, str, object]] = {}
        self._reminders: dict[AgentKey, _Reminder] = {}
        # latest blocked event per pane: (episode, prompt, prompt revision)
        self._prompts: dict[AgentKey, tuple[str, str | None, str | None]] = {}
        # episodes whose open event was handled (or open at start: baseline)
        self._known: OrderedDict[str, None] = OrderedDict()
        self._answered: OrderedDict[str, None] = OrderedDict()
        self._events: list[dict] = []
        self._flush_handle = None
        self._tasks: set[asyncio.Task] = set()
        # delivery
        self._alert_store = TelegramAlertStore()
        self._token: str | None = None
        self._client: _GuardedClient | None = None
        self._interactor: TelegramInteractor | None = None
        self._interactor_sig: tuple | None = None
        self._generation = 0
        self._offset: int | None = None
        self._inbound_disabled_until: float | None = None
        self._last_error = ""
        self._last_error_inbound = False
        self._last_sent_at_ms = 0
        self._recent: list[dict] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._run_task: asyncio.Task | None = None
        self._closed = False
        self._last_status = self.status()

    # --- settings ---------------------------------------------------------------
    def _tg_settings(self) -> TelegramSettings:
        try:
            return self._tg.settings
        except Exception:  # the store only holds normalized documents
            return TelegramSettings()

    def _shared(self) -> SharedSettings:
        return shared_settings_of(self._settings)

    def agents(self) -> dict[AgentKey, AgentState]:
        """The latest AgentState per pane (for ``BridgeAgentControl``)."""
        return self._agents

    # --- status -----------------------------------------------------------------
    def status(self) -> dict:
        s = self._tg_settings()
        source = self._tg.token_source()
        if source is None:
            inbound = "off"
        elif self._inbound_disabled_until is not None:
            inbound = "disabled"
        else:
            inbound = "ok"
        return {
            "token": source,
            "active": bool(s.enabled and source is not None and s.chat_id),
            "inbound": inbound,
            "last_error": self._last_error,
            "last_sent_at_ms": self._last_sent_at_ms,
            "recent_chats": [dict(chat) for chat in self._recent],
        }

    def _check_status(self) -> None:
        status = self.status()
        if status == self._last_status:
            return
        self._last_status = status
        callback = self.on_status_change
        if callback is not None:
            try:
                callback()
            except Exception:
                log.warning("telegram status listener failed", exc_info=True)

    def refresh(self) -> None:
        """The Telegram document or token changed: rebuild the bot client /
        interactor now and wake the poller."""
        self._sync()
        if self._wake is not None:
            self._wake.set()
        self._check_status()

    # --- the Bot API client ---------------------------------------------------------
    def _report(self, api: str, result: object, error: str | None) -> None:
        """Any thread: hand a Bot API outcome to the event loop."""
        loop = self._loop
        if loop is None:
            self._on_api(api, result, error)
            return
        try:
            loop.call_soon_threadsafe(self._on_api, api, result, error)
        except RuntimeError:  # loop closed
            pass

    def _on_api(self, api: str, result: object, error: str | None) -> None:
        inbound = api == "getUpdates"
        if error is not None:
            self._last_error = f"{api}: {error}"[:LAST_ERROR_MAX]
            self._last_error_inbound = inbound
        else:
            if self._last_error and self._last_error_inbound == inbound:
                self._last_error = ""
            if api == "sendMessage":
                self._last_sent_at_ms = int(self._clock() * 1000)
            if inbound:
                self._record_chats(result)
        self._check_status()

    def _sync(self) -> None:
        """(Re)build the client and interactor for the current token and
        settings. A new interactor continues from the getUpdates cursor, so an
        update already acted on is never processed again."""
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        token = self._tg.token()
        s = self._tg_settings()
        if token != self._token:
            # Another bot: its cursor, chats and conflicts are not ours.
            self._token = token
            self._client = None
            self._generation += 1
            self._interactor, self._interactor_sig = None, None
            self._offset = None
            self._recent = []
            self._inbound_disabled_until = None
            if token:
                try:
                    client = self._client_factory(token)
                    self._client = _GuardedClient(client, token, self._report)
                except Exception as exc:
                    log.warning(
                        "telegram client unavailable: %s", scrub(str(exc), token)
                    )
        interactive = bool(
            self._client is not None
            and s.enabled
            and s.chat_id
            and s.interactive
            and s.allowed_user_ids
        )
        sig = (
            (token, s.chat_id, s.message_thread_id, tuple(s.allowed_user_ids), s.prompt_max_chars)
            if interactive
            else None
        )
        if sig == self._interactor_sig:
            return
        self._interactor_sig = sig
        self._generation += 1
        self._interactor = None
        if sig is not None:
            self._interactor = TelegramInteractor(
                self._client,
                self._control,
                chat_id=s.chat_id,
                message_thread_id=s.message_thread_id,
                allowed_user_ids=list(s.allowed_user_ids),
                store=self._alert_store,
                prompt_max_chars=s.prompt_max_chars,
                offset=self._offset,
            )

    def _record_chats(self, updates: object) -> None:
        if not isinstance(updates, list):
            return
        for update in updates:
            entry = _chat_entry(update)
            if entry is None:
                continue
            ident = (entry["chat_id"], entry["message_thread_id"])
            for old in self._recent:
                if (old["chat_id"], old["message_thread_id"]) == ident:
                    if not entry["topic_name"]:
                        entry["topic_name"] = old["topic_name"]
                    self._recent.remove(old)
                    break
            self._recent.insert(0, entry)
            del self._recent[RECENT_CHATS_MAX:]

    # --- inputs -------------------------------------------------------------------
    def observe_panes(self, panes: list[dict]) -> None:
        """Digest one full wire snapshot (never raises)."""
        if self._closed:
            return
        try:
            self._observe(panes)
        except Exception:
            log.warning("telegram notifier: snapshot not digested", exc_info=True)
        self._flush_events()

    def _observe(self, panes: list[dict]) -> None:
        now_ms = int(self._clock() * 1000)
        new: dict[AgentKey, AgentState] = {}
        for pane in panes:
            try:
                state = _pane_to_state(self._server_id, pane)
            except Exception:
                continue
            new[state.key] = state
        previous = self._agents
        recycled = {
            key
            for key, state in new.items()
            if previous.get(key) is not None
            and previous[key].terminal_id
            and state.terminal_id
            and previous[key].terminal_id != state.terminal_id
        }
        gone = previous.keys() - new.keys()
        self._runs.forget(gone)
        self._bursts.forget(gone)
        for key in gone | recycled:
            self._cancel_pending_done(key)
            self._cancel_reminder(key)
            self._prompts.pop(key, None)
        for key in recycled:  # a recycled pane is a new agent
            self._runs.forget((key,))
            self._throttle.forget(key)
            self._bursts.forget((key,))
        self._agents = new
        for state in new.values():
            self._runs.observe(state, now_ms)
        if not self._observed:
            # What is open when the notifier starts (a bridge restart) was
            # alerted by whoever ran before: a silent baseline, as a runtime's
            # first event subscription.
            self._observed = True
            for state in new.values():
                if state.episode_id and state.status in (Status.BLOCKED, Status.DONE):
                    self._note_known(state.episode_id)
        shared = self._shared()
        if shared.subagents_done:
            for state in new.values():
                count = self._bursts.observe(state)
                if count is not None:
                    self._fire_subagents_done(state, count)

    def on_event(self, frame: dict) -> None:
        """``EventHub`` listener. Handled after the snapshot that carries the
        transition was observed (same loop step), so agent states match."""
        if self._closed or not isinstance(frame, dict):
            return
        self._events.append(frame)
        if self._flush_handle is not None:
            return
        try:
            self._flush_handle = asyncio.get_running_loop().call_soon(self._flush_events)
        except RuntimeError:
            self._flush_events()

    def _flush_events(self) -> None:
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        events, self._events = self._events, []
        for frame in events:
            try:
                self._handle_event(frame)
            except Exception:
                log.warning("telegram notifier: event not handled", exc_info=True)

    def _handle_event(self, frame: dict) -> None:
        kind = frame.get("kind")
        pane_id = frame.get("pane_id")
        episode = frame.get("episode_id")
        if not isinstance(pane_id, str) or not isinstance(episode, str) or not episode:
            return
        key = AgentKey(self._server_id, pane_id)
        at_ms = frame.get("at_ms") if type(frame.get("at_ms")) is int else None
        if kind == "blocked":
            self._ev_blocked(key, episode, frame, at_ms)
        elif kind == "done":
            fresh = episode not in self._known
            self._note_known(episode)
            state = self._agents.get(key)
            if fresh and _in_episode(state, Status.DONE, episode):
                self._fire("done", state, at_ms=at_ms)
        elif kind == "unblocked":
            reminder = self._reminders.get(key)
            if reminder is not None and reminder.episode == episode:
                self._cancel_reminder(key)
            if self._prompts.get(key, ("",))[0] == episode:
                self._prompts.pop(key, None)
        elif kind == "cleared":
            entry = self._pending_done.get(key)
            if entry is not None and entry[1] == episode:
                self._cancel_pending_done(key)
        elif kind == "answered":
            self._answered[episode] = None
            self._answered.move_to_end(episode)
            while len(self._answered) > _ANSWERED_EPISODES_MAX:
                self._answered.popitem(last=False)
            reminder = self._reminders.get(key)
            if reminder is not None and reminder.episode == episode:
                self._cancel_reminder(key)
            # A "done" right after an answer is the answer's own result.
            self._throttle.note_interaction(key)
            log.info("episode answered agent=%s:%s by=%s", key.server_id, pane_id,
                     _label(frame.get("by"), 64) or "?")

    def _ev_blocked(self, key: AgentKey, episode: str, frame: dict, at_ms: int | None) -> None:
        state = self._agents.get(key)
        if not _in_episode(state, Status.BLOCKED, episode):
            return
        prompt = frame.get("prompt") if isinstance(frame.get("prompt"), str) else None
        revision = frame.get("prompt_revision")
        revision = revision if isinstance(revision, str) else None
        previous = self._prompts.get(key)
        self._prompts[key] = (episode, prompt, revision)
        # The same episode asks a new question after it was answered: it
        # needs an answer (and an alert) again.
        reopened = (
            previous is not None
            and previous[0] == episode
            and previous[2] != revision
            and episode in self._answered
        )
        if reopened:
            del self._answered[episode]
        fresh = episode not in self._known
        self._note_known(episode)
        if fresh or reopened:
            self._fire("blocked", state, at_ms=at_ms)

    def _note_known(self, episode: str) -> None:
        self._known[episode] = None
        self._known.move_to_end(episode)
        while len(self._known) > _KNOWN_EPISODES_MAX:
            self._known.popitem(last=False)

    # --- decisions (the runtime's rules) ----------------------------------------------
    def _fire(
        self, event: str, state: AgentState, *, at_ms: int | None = None, deferred: bool = False
    ) -> None:
        shared = self._shared()
        if event not in shared.on:
            return
        if event == "done" and not deferred and self._quiet_done(state, shared):
            return
        if event == "blocked":
            self._track_reminder(state, at_ms, shared)
        if not self._throttle.allow(event, state.key):
            log.info(
                "telegram alert suppressed (cooldown/recent answer) event=%s agent=%s:%s",
                event, state.key.server_id, state.key.pane_id,
            )
            return
        lang = self._tg_settings().language
        title = event_title(state.agent_type, event, lang)
        self._spawn(self._deliver(event, state, title, state.episode_id))

    def _quiet_done(self, state: AgentState, shared: SharedSettings) -> bool:
        """done_min_work: True when this "done" must not alert now — a short
        run is dropped (done_short_delay 0) or re-checked later. An unknown
        run length counts as long."""
        if shared.done_min_work <= 0:
            return False
        run = self._runs.last(state.key)
        if run is None or run[1] is None or run[1] >= shared.done_min_work * 60_000:
            return False
        done_since, run_ms = run
        key = state.key
        if shared.done_short_delay <= 0:
            log.info("telegram done alert suppressed (short run %ds) agent=%s:%s",
                     run_ms // 1000, key.server_id, key.pane_id)
            return True
        due_ms = done_since + shared.done_short_delay * 60_000
        remaining = max(0.0, (due_ms - self._clock() * 1000) / 1000.0)
        self._cancel_pending_done(key)
        handle = self._timer(remaining, lambda: self._deferred_done(key, done_since))
        self._pending_done[key] = (done_since, state.episode_id, handle)
        log.info("telegram done alert deferred %.0fs (short run %ds) agent=%s:%s",
                 remaining, run_ms // 1000, key.server_id, key.pane_id)
        return True

    def _deferred_done(self, key: AgentKey, done_since: int) -> None:
        if self._closed:
            return
        entry = self._pending_done.get(key)
        if entry is not None and entry[0] == done_since:
            del self._pending_done[key]
        state = self._agents.get(key)
        run = self._runs.last(key)
        if state is None or state.status is not Status.DONE or run is None or run[0] != done_since:
            return
        self._fire("done", state, deferred=True)

    def _cancel_pending_done(self, key: AgentKey) -> None:
        entry = self._pending_done.pop(key, None)
        if entry is not None:
            entry[2].cancel()

    # --- reminders (shared remind_after) -------------------------------------------
    def _track_reminder(
        self, state: AgentState, at_ms: int | None, shared: SharedSettings
    ) -> None:
        """A block episode alerted: schedule its reminders, counted from when
        the pane blocked; ones that fell due before are not sent late."""
        interval = shared.remind_after * 60.0
        if interval <= 0 or not state.episode_id:
            return
        now = self._clock()
        elapsed = max(0.0, now - at_ms / 1000.0) if at_ms is not None else 0.0
        self._cancel_reminder(state.key)
        reminder = _Reminder(state.episode_id, now - elapsed, int(elapsed // interval))
        self._reminders[state.key] = reminder
        self._schedule_reminder(state.key, reminder, interval)

    def _schedule_reminder(self, key: AgentKey, reminder: _Reminder, interval: float) -> None:
        due = reminder.since + interval * (reminder.sent + 1)
        reminder.handle = self._timer(
            max(0.0, due - self._clock()), lambda: self._reminder_due(key, reminder)
        )

    def _cancel_reminder(self, key: AgentKey) -> None:
        reminder = self._reminders.pop(key, None)
        if reminder is not None and reminder.handle is not None:
            reminder.handle.cancel()

    def _reminder_due(self, key: AgentKey, reminder: _Reminder) -> None:
        if self._closed or self._reminders.get(key) is not reminder:
            return
        state = self._agents.get(key)
        interval = self._shared().remind_after * 60.0
        if (
            not _in_episode(state, Status.BLOCKED, reminder.episode)
            or reminder.episode in self._answered
            or reminder.sent >= REMIND_MAX
            or interval <= 0
        ):
            del self._reminders[key]
            return
        elapsed = self._clock() - reminder.since
        if elapsed >= interval * (reminder.sent + 1):
            reminder.sent += 1
            title = tr(
                self._tg_settings().language,
                "notify.title_reminder",
                agent=state.agent_type or "agent",
                minutes=int(elapsed // 60),
            )
            log.info("telegram reminder agent=%s:%s minutes=%d",
                     key.server_id, key.pane_id, int(elapsed // 60))
            self._spawn(self._deliver("blocked", state, title, reminder.episode))
        if reminder.sent >= REMIND_MAX:
            del self._reminders[key]
            return
        self._schedule_reminder(key, reminder, interval)

    # --- subagents_done -----------------------------------------------------------
    def _fire_subagents_done(self, state: AgentState, count: int) -> None:
        if not self._throttle.allow("subagents_done", state.key):
            log.info("telegram alert suppressed (cooldown) event=subagents_done agent=%s:%s",
                     state.key.server_id, state.key.pane_id)
            return
        title = tr(
            self._tg_settings().language,
            "notify.title_subagents_done",
            agent=state.agent_type or "agent",
            count=count,
        )
        self._spawn(self._deliver("subagents_done", state, title, ""))

    # --- delivery -------------------------------------------------------------------
    def _spawn(self, coro) -> None:
        if self._closed:
            coro.close()
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            log.warning("telegram alert dropped: no event loop")
            return
        task = loop.create_task(self._guard(coro))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guard(self, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never into the loop
            _warn_failure("telegram (bridge)", TelegramSendError(
                f"{type(exc).__name__}: {scrub(str(exc), self._token)}"
            ))

    def _away(self, s: TelegramSettings) -> bool:
        """only_when_away: nobody touched any Mac reporting to this bridge for
        that long. No fresh reporter at all (every Mac asleep) is away."""
        if s.only_when_away <= 0 or self._presence is None:
            return True
        idle, _fresh = self._presence.aggregate()
        return idle is None or idle >= s.only_when_away * 60.0

    def _alert_current(self, event: str, key: AgentKey, episode: str) -> bool:
        state = self._agents.get(key)
        if event == "blocked":
            return _in_episode(state, Status.BLOCKED, episode) and episode not in self._answered
        if event == "done":
            return _in_episode(state, Status.DONE, episode)
        return state is not None

    async def _deliver(self, event: str, state: AgentState, title: str, episode: str) -> None:
        key = state.key
        self._sync()
        s = self._tg_settings()
        if not (s.enabled and s.chat_id and self._client is not None):
            return
        if not self._alert_current(event, key, episode):
            log.info("telegram alert dropped (agent already left %s) agent=%s:%s",
                     event, key.server_id, key.pane_id)
            return
        if not self._away(s):
            log.info("telegram alert skipped (user at a Mac) event=%s agent=%s:%s",
                     event, key.server_id, key.pane_id)
            return
        body = event_notification_body(state, multi_server=False)
        if event == "blocked" and self._interactor is not None:
            await self._interactor.notify_blocked(
                state, body=body, sound=s.sound, multi_server=False
            )
            return
        text = f"{title}\n{body}"
        if event == "blocked":
            entry = self._prompts.get(key)
            prompt = entry[1] if entry is not None and entry[0] == episode else None
            excerpt = prompt_excerpt(prompt, s.prompt_max_chars) if prompt else ""
            if excerpt:
                text = f"{text}\n{excerpt}"
        log.info("telegram alert event=%s agent=%s:%s", event, key.server_id, key.pane_id)
        await self._send(text, s)

    async def _send(self, text: str, s: TelegramSettings) -> tuple[bool, str]:
        client = self._client
        if client is None:
            return False, "no bot token"
        try:
            await asyncio.to_thread(
                client.send_message,
                chat_id=s.chat_id,
                text=text,
                sound=s.sound,
                message_thread_id=s.message_thread_id,
            )
        except Exception as exc:
            message = scrub(str(exc) or type(exc).__name__, self._token)
            _warn_failure("telegram (bridge)", TelegramSendError(message))
            return False, message
        return True, ""

    async def send_test(self) -> tuple[bool, str]:
        """Send the i18n test message to the configured chat/topic now
        (whether or not alerts are enabled). (ok, token-free error text)."""
        self._sync()
        s = self._tg_settings()
        if self._client is None:
            return False, "no bot token"
        if not s.chat_id:
            return False, "no chat_id"
        result = await self._send(tr(s.language, "telegram.test"), s)
        self._check_status()
        return result

    # --- the poller -----------------------------------------------------------------
    async def poll_step(self) -> float:
        """One getUpdates round (interactive, or chat discovery); returns how
        long to wait before the next one. Never raises (but cancellation)."""
        if self._closed:
            return IDLE_WAIT_S
        try:
            return await self._poll()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _warn_failure("telegram poll (bridge)", TelegramSendError(
                f"{type(exc).__name__}: {scrub(str(exc), self._token)}"
            ))
            self._check_status()
            return POLL_ERROR_BACKOFF_S

    async def _poll(self) -> float:
        now = self._clock()
        if self._inbound_disabled_until is not None:
            if now < self._inbound_disabled_until:
                return self._inbound_disabled_until - now
            self._inbound_disabled_until = None
            self._interactor_sig = None  # a fresh interactor (its 409 flag reset)
        self._sync()
        client = self._client
        if client is None:
            self._check_status()
            return IDLE_WAIT_S
        generation = self._generation
        interactor = self._interactor
        try:
            if interactor is not None:
                await self._poll_interactive(interactor, generation)
                if interactor.inbound_disabled:
                    return self._inbound_conflict()
            else:
                await self._poll_discovery(client, generation)
        except TelegramApiError as exc:
            if exc.error_code == 409:
                return self._inbound_conflict()
            raise
        self._check_status()
        return 0.0

    async def _poll_interactive(self, interactor: TelegramInteractor, generation: int) -> None:
        if self._offset is not None and (interactor.offset is None or interactor.offset < self._offset):
            interactor._offset = self._offset  # noqa: SLF001 - cursor carry-over
        try:
            await interactor.poll_once(
                timeout=self._poll_timeout,
                is_current=lambda: generation == self._generation and not self._closed,
            )
        finally:
            offset = interactor.offset
            if offset is not None and (self._offset is None or offset > self._offset):
                self._offset = offset

    async def _poll_discovery(self, client: _GuardedClient, generation: int) -> None:
        """No interactive chain: read updates only to learn chats (recorded
        by the client report); commands are ignored, nothing is sent. A poll
        that outlived a rebuild leaves its updates to the new mode."""
        updates = await asyncio.to_thread(
            client.get_updates, offset=self._offset, timeout=self._poll_timeout
        )
        if generation != self._generation or self._closed or not isinstance(updates, list):
            return
        for update in updates:
            update_id = update.get("update_id") if isinstance(update, dict) else None
            if type(update_id) is int and (self._offset is None or update_id + 1 > self._offset):
                self._offset = update_id + 1

    def _inbound_conflict(self) -> float:
        """409: a webhook is set, or another poller uses this bot."""
        self._inbound_disabled_until = self._clock() + INBOUND_RETRY_S
        log.warning("telegram inbound disabled for %.0fs: %s", INBOUND_RETRY_S,
                    self._last_error or "conflict (409)")
        self._check_status()
        return INBOUND_RETRY_S

    async def _wait(self, seconds: float) -> None:
        if self._wake is None:
            self._wake = asyncio.Event()
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except TimeoutError:
            pass
        self._wake.clear()

    async def run(self) -> None:
        """The poller loop; survives any iteration's failure."""
        self._run_task = asyncio.current_task()
        while not self._closed:
            try:
                delay = await self.poll_step()
            except asyncio.CancelledError:
                raise
            except Exception:  # poll_step already guards; belt and braces
                log.warning("telegram poller iteration failed")
                delay = POLL_ERROR_BACKOFF_S
            if delay > 0 and not self._closed:
                await self._sleep(delay)
            else:
                await asyncio.sleep(0)

    async def flush(self) -> None:
        """Handle queued events and wait for the alerts in flight."""
        self._flush_events()
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        await asyncio.sleep(0)

    async def close(self) -> None:
        """Stop timers, alerts and the poller at once. An in-flight
        getUpdates thread cannot be cancelled and is not waited for."""
        self._closed = True
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        self._events.clear()
        for key in list(self._pending_done):
            self._cancel_pending_done(key)
        for key in list(self._reminders):
            self._cancel_reminder(key)
        if self._wake is not None:
            self._wake.set()
        tasks = list(self._tasks)
        run_task = self._run_task
        if run_task is not None and run_task is not asyncio.current_task():
            tasks.append(run_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=1.0)
