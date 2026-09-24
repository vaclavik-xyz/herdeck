"""Asyncio-side services of the one runtime: agent control, the versioned
cockpit API and interactive Telegram alerts.

They used to live in the legacy ``herdeck.app.App``; here they sit on top of the
DeckApp's current source (``LiveSource``) instead of a second orchestrator:

* ``RuntimeAgentControl`` issues bridge requests through the source's connector
  runners and gets its results back through the source's result tap;
* ``SemanticAPI`` (``/api/v1/*`` of the web cockpit) reads the source's agents,
  per-server readiness and per-agent generations;
* ``TelegramInteractor`` receives blocked alerts from the source's notification
  engine (``LiveSource.set_telegram_interactive``) and long-polls the Bot API.

Everything asynchronous runs on one private event loop thread. The current
source is looked up per call, so a config reload (DeckApp.swap_source) needs
only ``wire(new_source)`` before the swap.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Callable

from ..app_control import RuntimeAgentControl
from ..commands import Command, command_to_msg
from ..config import Config
from ..model import AgentKey, AgentState
from ..secrets import get_secret
from ..semantic_api import SemanticAPI
from ..telegram import TelegramAlertStore, TelegramBotClient, TelegramInteractor

log = logging.getLogger(__name__)

TELEGRAM_POLL_TIMEOUT_S = 20


class _DaemonThreadExecutor(concurrent.futures.ThreadPoolExecutor):
    """Default executor of the services loop: one daemon thread per call (a
    ThreadPoolExecutor subclass only because asyncio insists on one).

    The Bot API calls (``asyncio.to_thread``: a 20 s ``getUpdates`` long poll)
    cannot be cancelled. On the stock executor their non-daemon workers are
    joined at interpreter exit, so SIGTERM could wait out a whole long poll
    (and launchd's kill timeout). Daemon threads let the process exit now."""

    def submit(self, fn, /, *args, **kwargs):
        future: concurrent.futures.Future = concurrent.futures.Future()

        def run() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as exc:  # noqa: BLE001 - handed to the awaiter
                future.set_exception(exc)

        threading.Thread(target=run, name="herdeck-services-io", daemon=True).start()
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        return None


class RuntimeServices:
    def __init__(
        self,
        config: Config,
        *,
        current_source: Callable[[], object],
        getenv=get_secret,
        bot_client_factory=TelegramBotClient,
        interactor_factory=TelegramInteractor,
        telegram_poll_timeout: int = TELEGRAM_POLL_TIMEOUT_S,
    ):
        self._current_source = current_source
        self._getenv = getenv
        self._bot_client_factory = bot_client_factory
        self._interactor_factory = interactor_factory
        self._poll_timeout = telegram_poll_timeout
        # Bumped on every wired source: a source's own agent generations start
        # from zero, so the pair (wire generation, agent generation) stays unique
        # and a stop confirmation never survives a reload.
        self._wire_generation = 0
        self._tg_store = TelegramAlertStore()
        self._tg_signature: tuple | None = None
        self._tg_warned = False
        self._interactor = None
        self._interactor_generation = 0
        self._closing = False
        self._loop = asyncio.new_event_loop()
        self._loop.set_default_executor(_DaemonThreadExecutor())
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._serve, name="herdeck-runtime-services", daemon=True
        )
        self._thread.start()
        self._ready.wait(5)
        self.control = RuntimeAgentControl(
            config, send=self._send, current_agent=self._current_agent
        )
        self.semantic = SemanticAPI(
            self.control,
            agents=self._agents,
            server_available=self._server_available,
            generation=self._generation,
        )
        self._update_telegram(config)
        self._poll_future = asyncio.run_coroutine_threadsafe(self._poll_telegram(), self._loop)

    # --- loop ------------------------------------------------------------------
    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._ready.set)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._poll_future.cancel()
        # An in-flight getUpdates long poll (a daemon thread, up to 20 s)
        # cannot be cancelled: do not wait for it.

        async def drain() -> None:
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        try:
            asyncio.run_coroutine_threadsafe(drain(), self._loop).result(1)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=1)

    # --- source wiring -----------------------------------------------------------
    def wire(self, source) -> None:
        """Hook a source (before it becomes current): its result tap and the
        interactive Telegram chain, and adopt its config."""
        self._wire_generation += 1
        tap = getattr(source, "set_result_tap", None)
        if callable(tap):
            tap(self.claim_result)
        interactive = getattr(source, "set_telegram_interactive", None)
        if callable(interactive):
            interactive(self.telegram_active, self.notify_blocked)
        config = getattr(source, "config", None)
        if config is not None:
            self.control.update_config(config)
            self._update_telegram(config)

    def _source(self):
        return self._current_source()

    def _agents(self) -> list[AgentState]:
        agents = getattr(self._source(), "semantic_agents", None)
        return list(agents()) if callable(agents) else []

    def _current_agent(self, key: AgentKey) -> AgentState | None:
        agent = getattr(self._source(), "semantic_agent", None)
        return agent(key) if callable(agent) else None

    def _server_available(self, server_id: str) -> bool:
        available = getattr(self._source(), "semantic_server_available", None)
        return bool(available(server_id)) if callable(available) else False

    def _generation(self, server_id: str, pane_id: str) -> tuple[int, int]:
        generation = getattr(self._source(), "semantic_generation", None)
        return (
            self._wire_generation,
            generation(server_id, pane_id) if callable(generation) else 0,
        )

    async def _send(self, command: Command, req: str) -> None:
        source = self._source()
        local = getattr(source, "control_result", None)
        if callable(local):
            # A source without a bridge (the demo deck) answers in-process.
            data = local(command)
            self._loop.call_soon(self._deliver, command.server_id, req, data)
            return
        runner = getattr(source, "_runners", {}).get(command.server_id)
        if runner is not None:
            runner.send(command_to_msg(command, req))

    def _deliver(self, server_id: str, req: str, data: dict) -> None:
        self.control.handle_result(req, data, server_id=server_id)

    def claim_result(self, server_id: str, req: str, data: dict) -> Command | None:
        """Connector-thread result tap: a result of a control request is handed
        to the services loop and reported as claimed (its command)."""
        command = self.control.owns(req, server_id)
        if command is None:
            return None
        self._loop.call_soon_threadsafe(self._deliver, server_id, req, data)
        return command

    # --- cockpit API ---------------------------------------------------------------
    def semantic_request(self, request: dict) -> concurrent.futures.Future:
        """Entry point for the web front's /api/v1 routes (any thread)."""
        return asyncio.run_coroutine_threadsafe(self.semantic.handle(request), self._loop)

    # --- interactive Telegram ------------------------------------------------------
    def _update_telegram(self, config: Config) -> None:
        n = config.notifications
        tg = n.telegram
        requested = bool(
            n.enabled and tg is not None and "telegram" in n.backends and tg.interactive
        )
        token = self._getenv(tg.token_env) if requested else None
        signature = None
        if requested and token and tg.chat_id and tg.allowed_user_ids:
            signature = (
                token,
                str(tg.chat_id),
                tg.message_thread_id,
                tuple(tg.allowed_user_ids),
                tg.prompt_max_chars,
            )
        elif requested and not self._tg_warned:
            self._tg_warned = True
            log.warning(
                "interactive telegram needs a bot token, chat_id and allowed_user_ids; "
                "keeping one-way telegram alerts"
            )
        if signature == self._tg_signature:
            return
        self._tg_signature = signature
        previous = self._interactor
        interactor = None
        if signature is not None:
            interactor = self._interactor_factory(
                self._bot_client_factory(token),
                self.control,
                chat_id=tg.chat_id,
                message_thread_id=tg.message_thread_id,
                allowed_user_ids=tg.allowed_user_ids,
                prompt_max_chars=tg.prompt_max_chars,
                store=self._tg_store,
                # continue from the old cursor: never re-run acted-on updates
                offset=previous.offset if previous is not None else None,
            )
        self._interactor = interactor
        self._interactor_generation += 1

    def telegram_active(self) -> bool:
        return self._interactor is not None

    def notify_blocked(
        self, agent: AgentState, *, body: str, sound, multi_server: bool
    ) -> None:
        """Notify-thread hook: send the interactive blocked alert (async)."""
        interactor = self._interactor
        if interactor is None or self._closing:
            return
        future = asyncio.run_coroutine_threadsafe(
            interactor.notify_blocked(agent, body=body, sound=sound, multi_server=multi_server),
            self._loop,
        )
        future.add_done_callback(_log_failure)

    async def _poll_telegram(self) -> None:
        previous = None
        while True:
            interactor = self._interactor
            generation = self._interactor_generation
            if interactor is None:
                await asyncio.sleep(1)
                continue
            if previous is not None and previous is not interactor:
                # the old interactor may have advanced its cursor after the
                # new one copied it (a poll that was in flight): catch up
                old, new = previous.offset, interactor.offset
                if old is not None and (new is None or new < old):
                    interactor._offset = old
            previous = interactor
            if getattr(interactor, "inbound_disabled", False):
                await asyncio.sleep(60)
                continue
            try:
                await interactor.poll_once(
                    timeout=self._poll_timeout,
                    is_current=lambda gen=generation: self._interactor_generation == gen,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("telegram poll failed", exc_info=True)
                await asyncio.sleep(2)


def _log_failure(future: concurrent.futures.Future) -> None:
    if future.cancelled():
        return
    exc = future.exception()
    if exc is not None:
        log.debug("interactive telegram alert failed", exc_info=exc)
