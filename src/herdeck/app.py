from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import queue
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .app_control import RuntimeAgentControl
from .bootstrap import (
    _discover_config_path,
    _discover_local_config_path,
    make_runtime_profile_switcher,
    resolve_mode,
    resolve_runtime_config,
)
from .bootstrap import (
    local_config as local_config,
)
from .commands import Command, command_to_msg
from .config import Config, ConfigError, ServerConfig
from .connector import Connector
from .driver.base import DeckDriver, PanelView
from .driver.fake import FakeRenderer
from .i18n import tr
from .model import AgentState, Status
from .notify import (
    BlockedAlertNotifier,
    BlockedNotificationRuntime,
    CompositeBlockedNotifier,
    LegacyBlockedNotifier,
    NoopNotifier,
    Notifier,
    _macos_sink,
    composite_sink,
    make_telegram_sink,
)
from .orchestrator import Orchestrator
from .protocol import TermClosed, TermFrame
from .secrets import get_secret
from .telegram import TelegramBotClient, TelegramInteractor

TICK_INTERVAL = 0.4
# Every Nth tick, fully re-render so elapsed-time text on non-working tiles
# (idle/blocked/done) advances even without a status change. 25 * 0.4s ≈ 10s.
FULL_REFRESH_TICKS = 25
# A status panel ("reload failed", "profile locked") stays visible this long —
# without a hold the very next refresh overwrote it within one 0.4s tick and
# the user never learned why their action had no effect.
STATUS_PANEL_HOLD_S = 4.0

# Module-level indirection so tests can fake the clock.
_monotonic = None  # set lazily to time.monotonic (keeps import cost at top low)


def _now() -> float:
    global _monotonic
    if _monotonic is None:
        import time

        _monotonic = time.monotonic
    return _monotonic()

log = logging.getLogger("herdeck")


def newly_blocked(prev, states):
    """Keys that just entered BLOCKED (vs prev), and the updated blocked set.
    Eligibility resets when a key leaves BLOCKED, so a re-block notifies again."""
    blocked_now = {s.key for s in states if s.status is Status.BLOCKED}
    to_notify = blocked_now - prev
    return to_notify, blocked_now


def _build_notifier(
    config: Config,
    *,
    getenv=get_secret,  # was os.environ.get — env-first/keychain via the shared resolver
    macos_sink=_macos_sink,
    telegram_factory=make_telegram_sink,
    skip_telegram: bool = False,
) -> Notifier:
    """Assemble a notifier from the configured backends (graceful skip)."""
    n = config.notifications
    if not n.enabled:
        return NoopNotifier()
    sinks = []
    for backend in n.backends:
        if backend == "macos":
            sinks.append(macos_sink)
        elif backend == "telegram":
            if skip_telegram:
                continue
            tg = n.telegram
            token = getenv(tg.token_env) if tg else None
            if tg and token and tg.chat_id:
                sinks.append(telegram_factory(token, tg.chat_id, tg.message_thread_id))
            else:
                log.warning(
                    "telegram notifications enabled but token/chat_id "
                    "missing; skipping telegram backend"
                )
        else:
            log.warning("unknown notification backend %r; skipping", backend)
    if not sinks:
        return NoopNotifier()
    return Notifier(sink=composite_sink(sinks))


def _build_blocked_notification_runtime(
    config: Config,
    *,
    getenv=get_secret,
    macos_sink=_macos_sink,
    telegram_factory=make_telegram_sink,
    telegram_interactor_factory=None,
) -> BlockedNotificationRuntime:
    n = config.notifications
    tg = n.telegram
    interactive_requested = (
        n.enabled
        and tg is not None
        and "telegram" in n.backends
        and tg.interactive
        and telegram_interactor_factory is not None
    )
    interactive_token = getenv(tg.token_env) if interactive_requested else None
    interactive_enabled = bool(
        interactive_requested
        and interactive_token
        and tg is not None
        and tg.chat_id
        and tg.allowed_user_ids
    )
    if interactive_enabled:
        assert tg is not None
        assert interactive_token is not None
        interactor = telegram_interactor_factory(interactive_token, tg)
        poll_once = getattr(interactor, "poll_once", None)
        if callable(poll_once):
            legacy = _build_notifier(
                config,
                getenv=getenv,
                macos_sink=macos_sink,
                telegram_factory=telegram_factory,
                skip_telegram=True,
            )
            notifiers: list[BlockedAlertNotifier] = [LegacyBlockedNotifier(legacy), interactor]
            notifier = (
                notifiers[0] if len(notifiers) == 1 else CompositeBlockedNotifier(notifiers)
            )
            return BlockedNotificationRuntime(notifier, interactor)
        log.warning(
            "interactive telegram notifications requested but inbound poller "
            "is unavailable; keeping one-way telegram backend"
        )

    legacy = _build_notifier(
        config,
        getenv=getenv,
        macos_sink=macos_sink,
        telegram_factory=telegram_factory,
    )
    notifiers = [LegacyBlockedNotifier(legacy)]
    notifier = notifiers[0] if len(notifiers) == 1 else CompositeBlockedNotifier(notifiers)
    return BlockedNotificationRuntime(notifier, None)


def _build_blocked_notifier(*args, **kwargs) -> BlockedAlertNotifier:
    return _build_blocked_notification_runtime(*args, **kwargs).notifier


async def _guard_blocked_notify(coro: Awaitable[None]) -> None:
    try:
        await coro
    except Exception:
        log.debug("blocked alert notifier failed", exc_info=True)


def _default_notify_schedule(coro: Awaitable[None]) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_guard_blocked_notify(coro))
    else:
        loop.create_task(_guard_blocked_notify(coro))


@dataclass
class TermSub:
    """One browser terminal subscription, drained by the HTTP thread."""

    req: str
    queue: queue.Queue[dict]
    server_id: str | None = None
    pane_id: str | None = None


class App:
    """Glue between orchestrator (sync) and connectors (async)."""

    def __init__(
        self,
        config: Config,
        deck: DeckDriver,
        send: Callable[[Command], None],
        schedule: Callable[[Callable[[], None]], None] | None = None,
        notifier: Notifier | None = None,
        blocked_notifier: BlockedAlertNotifier | None = None,
        blocked_runtime_factory: Callable[[Config], BlockedNotificationRuntime] | None = None,
        notify_schedule: Callable[[Awaitable[None]], None] | None = None,
        switch_profile: Callable[[str], Config | None] | None = None,
        update_connectors: Callable[[Config], object] | None = None,
        config_reloader: Callable[[], Config] | None = None,
        runtime_control: RuntimeAgentControl | None = None,
        send_raw: Callable[[str, dict], bool] | None = None,
    ):
        self.config = config
        self.deck = deck
        self._send = send
        self._schedule = schedule or (lambda fn: fn())
        self._switch_profile = switch_profile
        self._update_connectors = update_connectors or (lambda cfg: None)
        self._config_reloader = config_reloader
        self._runtime_control = runtime_control
        self._send_raw = send_raw
        # All subscription lifecycle changes happen on the asyncio loop. The
        # queue in TermSub is the only state shared with the HTTP thread.
        self._terminals: dict[str, TermSub] = {}
        self._servers_up: set[str] = set()
        self.notifier = notifier or NoopNotifier()
        self._blocked_runtime_factory = blocked_runtime_factory
        self.notification_poller = None
        self._notification_generation = 0
        if blocked_notifier is not None:
            runtime = BlockedNotificationRuntime(blocked_notifier)
            self._blocked_runtime_factory = lambda config: runtime
            self._install_blocked_runtime(runtime)
        elif blocked_runtime_factory is not None:
            self._install_blocked_runtime(blocked_runtime_factory(config))
        else:
            self._install_blocked_runtime(
                BlockedNotificationRuntime(LegacyBlockedNotifier(self.notifier))
            )
        self._notify_schedule = notify_schedule or _default_notify_schedule
        self._blocked_keys: set = set()
        self.orch = Orchestrator(config, slots=deck.slot_count())
        deck.on_press(self._on_press)
        on_terminal = getattr(deck, "on_terminal", None)
        if callable(on_terminal):
            on_terminal(self.open_terminal, self.close_terminal)
        self._req = 0
        self._active_read_req: str | None = None
        self._ticks = 0
        self._status_panel: PanelView | None = None
        self._status_panel_until = 0.0
        # CodexBar usage poller (None when [usage] is off); renders read its
        # latest snapshot only, never block on the CLI.
        self._usage_cfg = getattr(config, "usage", None)
        self._usage_poller = self._build_usage_poller(self._usage_cfg)

    def _install_blocked_runtime(self, runtime: BlockedNotificationRuntime) -> None:
        self._notification_generation += 1
        self.blocked_notifier = runtime.notifier
        self.notification_poller = runtime.poller

    @property
    def notification_generation(self) -> int:
        return self._notification_generation

    def _rebuild_blocked_runtime(self, config: Config) -> None:
        if self._blocked_runtime_factory is not None:
            self._install_blocked_runtime(self._blocked_runtime_factory(config))
        else:
            self._install_blocked_runtime(
                BlockedNotificationRuntime(LegacyBlockedNotifier(self.notifier))
            )

    def set_blocked_runtime_factory(
        self, factory: Callable[[Config], BlockedNotificationRuntime]
    ) -> None:
        self._blocked_runtime_factory = factory
        self._rebuild_blocked_runtime(self.config)

    def set_runtime_control(self, runtime_control: RuntimeAgentControl | None) -> None:
        self._runtime_control = runtime_control

    def next_req_for(self, cmd: Command) -> str | None:
        if cmd.kind == "list":
            return None
        self._req += 1
        req = f"r{self._req}"
        if cmd.kind == "read":
            self._active_read_req = req
        return req

    def _held_status_panel(self) -> PanelView | None:
        """The active status panel while its hold lasts, else None."""
        if self._status_panel is not None and _now() < self._status_panel_until:
            return self._status_panel
        self._status_panel = None
        return None

    @staticmethod
    def _build_usage_poller(usage_cfg):
        from .usage import poller_from_config

        poller = poller_from_config(usage_cfg)
        if poller is not None:
            poller.start()
        return poller

    def _adopt_usage_config(self, config: Config) -> None:
        """Rebuild the poller when a reload/profile switch changed [usage] —
        else disabling it left the old thread polling the CLI forever and
        enabling it needed a process restart (deckapp got this in
        _adopt_usage_config; this is the legacy-host mirror)."""
        new_cfg = getattr(config, "usage", None)
        if new_cfg == self._usage_cfg:
            return
        old = self._usage_poller
        self._usage_cfg = new_cfg
        self._usage_poller = self._build_usage_poller(new_cfg)
        if old is not None:
            with contextlib.suppress(Exception):
                old.close()

    def _refresh(self) -> None:
        # ALWAYS feed usage state (empty when off): the orchestrator may carry
        # usage lines from before a reload that disabled [usage].
        poller = self._usage_poller
        self.orch.set_usage(poller.snapshot() if poller is not None else [])
        rs = self.orch.render()
        held = self._held_status_panel()
        try:
            self.deck.render(rs.tiles)
            self.orch.confirm_rendered_preview()
            self.deck.render_panel(held if held is not None else rs.panel)
        except Exception:
            pass  # a render failure must never freeze the loop

    def _set_status_panel(self, title: str, lines: list[str], color: str = "grey") -> None:
        self._status_panel = PanelView(title, lines, color)
        self._status_panel_until = _now() + STATUS_PANEL_HOLD_S
        try:
            self.deck.render_panel(self._status_panel)
        except Exception:
            pass

    def _server_allowed(self, server_id: str) -> bool:
        return any(server.id == server_id for server in self.config.servers)

    def _invalidate_read(self) -> None:
        self._active_read_req = None
        self.orch.set_detection("")

    def _invalidate_read_if_unblocked(self, key) -> None:
        """Drop the drilled prompt only when the agent actually leaves BLOCKED.

        The prompt (and an in-flight read) stays valid as long as the agent
        remains blocked. Wiping on every cosmetic change instead made routine
        fleet snapshots reject the in-flight read (prompt never showed; "click
        3×") or clear an already-shown prompt ("shows then disappears").
        """
        agent = self.orch.get_agent(key)
        if agent is None or agent.status is not Status.BLOCKED:
            self._invalidate_read()

    def _maybe_notify(self, states: list[AgentState], scope: set) -> None:
        """Fire notifications for keys that just entered BLOCKED.

        `scope` is the set of tracked keys these `states` are authoritative for
        (a whole server for snapshots, a single key for events) — only that
        scope is reconciled, so other servers' blocked keys are never dropped.
        """
        if not self.config.notifications.enabled:
            return
        if "blocked" not in self.config.notifications.on:
            return
        prev_here = self._blocked_keys & scope
        to, blocked_here = newly_blocked(prev_here, states)
        self._blocked_keys = (self._blocked_keys - scope) | blocked_here
        multi = len(self.config.overview_order) > 1
        sound = self.config.notifications.sound
        for s in (x for x in states if x.key in to):
            self._schedule_blocked_notify(
                s,
                self._blocked_notification_body(s, multi_server=multi),
                sound,
                multi,
            )

    def _schedule_blocked_notify(
        self, agent: AgentState, body: str, sound: bool, multi_server: bool
    ) -> None:
        self._notify_schedule(
            self.blocked_notifier.notify_blocked(
                agent, body=body, sound=sound, multi_server=multi_server
            )
        )

    def _blocked_notification_body(self, agent: AgentState, *, multi_server: bool) -> str:
        label = agent.repo or agent.label
        parts = [
            part
            for part in (agent.branch, agent.key.server_id if multi_server else None)
            if part
        ]
        return f"{label}" + (f" · {' · '.join(parts)}" if parts else "")

    def _rearm_interactive_blocked_alerts(self) -> None:
        if not self.config.notifications.enabled:
            return
        if "blocked" not in self.config.notifications.on:
            return
        notify = getattr(self.notification_poller, "notify_blocked", None)
        if not callable(notify):
            return
        multi = len(self.config.overview_order) > 1
        sound = self.config.notifications.sound
        for agent in self.orch.agents():
            if agent.status is Status.BLOCKED:
                self._notify_schedule(
                    notify(
                        agent,
                        body=self._blocked_notification_body(agent, multi_server=multi),
                        sound=sound,
                        multi_server=multi,
                    )
                )

    def handle_snapshot(self, server_id: str, states: list[AgentState]) -> None:
        if not self._server_allowed(server_id):
            return
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "snapshot %s: %s",
                server_id,
                [(s.key.pane_id, s.agent_type, s.label, s.status.value) for s in states],
            )
        key = self.orch.drill_key()
        self.orch.apply_snapshot(server_id, states)
        if key is not None and key.server_id == server_id:
            self._invalidate_read_if_unblocked(key)
        self._maybe_notify(states, {k for k in self._blocked_keys if k.server_id == server_id})
        self._refresh()

    def handle_event(self, server_id: str, state: AgentState) -> None:
        if not self._server_allowed(server_id):
            return
        self.orch.apply_event(server_id, state)
        if self.orch.is_drill_pane(server_id, state.key.pane_id):
            self._invalidate_read_if_unblocked(state.key)
        self._maybe_notify([state], {state.key})
        self._refresh()

    def handle_connection(self, server_id: str, up: bool) -> None:
        if not self._server_allowed(server_id):
            return
        if up:
            self._servers_up.add(server_id)
        else:
            self._servers_up.discard(server_id)
            self._close_server_terminals(server_id)
        self.orch.set_connection(server_id, up)
        self._refresh()

    def handle_result(self, server_id: str, req: str, data: dict) -> None:
        if not self._server_allowed(server_id):
            return
        if self._runtime_control is not None:
            handled = self._runtime_control.handle_result(req, data, server_id=server_id)
            if handled is not None:
                if handled.kind != "read":
                    self._send(Command("list", handled.server_id))
                return
        text = data.get("text")
        if text is not None:
            accepted = req == self._active_read_req and self.orch.is_drill_pane(
                server_id, data.get("pane_id")
            )
            log.debug(
                "result read req=%s pane=%s accepted=%s text=%r",
                req,
                data.get("pane_id"),
                accepted,
                (text or "")[:60],
            )
            if accepted:
                self.orch.set_detection(text)
                self._refresh()
        else:
            log.debug("result act req=%s data=%s -> re-list", req, data)
            self._send(Command("list", server_id))

    _TERM_QUEUE_MAX = 120

    def open_terminal(self, index: int, cols: int, rows: int) -> TermSub:
        """Schedule a preview start from a non-loop thread."""
        sub = TermSub(
            req=f"t{uuid.uuid4().hex[:12]}",
            queue=queue.Queue(maxsize=self._TERM_QUEUE_MAX),
        )
        self._schedule(lambda: self._start_terminal(sub, index, cols, rows))
        return sub

    def close_terminal(self, sub: TermSub) -> None:
        """Schedule an idempotent preview stop from a non-loop thread."""
        self._schedule(lambda: self._stop_terminal(sub))

    def _term_lang(self) -> str:
        return self.config.view.language

    def _send_terminal(self, server_id: str, message: dict) -> bool:
        if self._send_raw is None:
            return False
        try:
            return bool(self._send_raw(server_id, message))
        except Exception:
            log.debug("terminal preview send failed", exc_info=True)
            return False

    def _start_terminal(self, sub: TermSub, index: int, cols: int, rows: int) -> None:
        self._terminals[sub.req] = sub
        agent = self.orch.agent_for_preview(index)
        if agent is None:
            self._finish_terminal(sub, tr(self._term_lang(), "web.term_no_agent"))
            return

        key = agent.key
        if key.server_id not in self._servers_up or self._send_raw is None:
            self._finish_terminal(sub, tr(self._term_lang(), "web.term_disconnected"))
            return

        sub.server_id = key.server_id
        sub.pane_id = key.pane_id
        sub.queue.put_nowait({"kind": "meta", "label": agent.label or agent.agent_type})
        started = self._send_terminal(
            key.server_id,
            {
                "type": "observe",
                "req": sub.req,
                "pane_id": key.pane_id,
                "cols": cols,
                "rows": rows,
            },
        )
        if not started:
            self._finish_terminal(sub, tr(self._term_lang(), "web.term_disconnected"))

    def _stop_terminal(self, sub: TermSub) -> None:
        if self._terminals.get(sub.req) is not sub:
            return
        del self._terminals[sub.req]
        if sub.server_id is not None:
            self._send_terminal(
                sub.server_id,
                {"type": "observe_stop", "req": sub.req},
            )

    def _finish_terminal(self, sub: TermSub, reason: str) -> None:
        if self._terminals.get(sub.req) is not sub:
            return
        del self._terminals[sub.req]
        closed = {"kind": "closed", "reason": reason}
        try:
            sub.queue.put_nowait(closed)
        except queue.Full:
            # Preserve bounded memory while guaranteeing a final close marker.
            with contextlib.suppress(queue.Empty):
                sub.queue.get_nowait()
            sub.queue.put_nowait(closed)

    def _close_server_terminals(self, server_id: str) -> None:
        for sub in list(self._terminals.values()):
            if sub.server_id == server_id:
                self._finish_terminal(
                    sub,
                    tr(self._term_lang(), "web.term_disconnected"),
                )

    def handle_term(self, server_id: str, message: TermFrame | TermClosed) -> None:
        """Route an inbound terminal frame on the asyncio loop."""
        sub = self._terminals.get(message.req)
        if sub is None or sub.server_id != server_id:
            return
        if isinstance(message, TermClosed):
            self._finish_terminal(
                sub,
                message.reason or tr(self._term_lang(), "web.term_ended"),
            )
            return

        frame = {
            "kind": "frame",
            "seq": message.seq,
            "full": message.full,
            "cols": message.cols,
            "rows": message.rows,
            "data": message.data,
        }
        try:
            sub.queue.put_nowait(frame)
        except queue.Full:
            self._finish_terminal(sub, tr(self._term_lang(), "web.term_ended"))
            self._send_terminal(
                server_id,
                {"type": "observe_stop", "req": sub.req},
            )

    def handle_tick(self) -> None:
        working = self.orch.tick()
        self._ticks += 1
        # Re-send the WHOLE frame whenever anything animates (or on the periodic
        # elapsed refresh). A partial render_working write drops the cells it omits on
        # the D200 firmware — blanking static/idle tiles + the panel, leaving only the
        # working tiles lit — so never send a partial frame. A full render is cheap now
        # that strmdck's retry sleep is neutralized (one combined write).
        if (
            working
            or self._ticks % FULL_REFRESH_TICKS == 0
            or self.orch.consume_expired_panel_hold()
        ):
            self._refresh()

    def _on_press(self, index: int) -> None:
        self._schedule(lambda: self._handle_press(index))

    def _handle_press(self, index: int) -> None:
        self._status_panel = None  # any key press dismisses a held status panel
        cmds = self.orch.on_press(index)
        if log.isEnabledFor(logging.DEBUG):
            rs = self.orch.render()
            labels = [t.label for t in rs.tiles[:6] if t.label]
            log.debug(
                "press idx=%s -> cmds=%s | view=%s panel=%r/%s",
                index,
                [(c.kind, c.pane_id, c.keys) for c in cmds],
                labels,
                rs.panel.title,
                rs.panel.lines,
            )
        for cmd in cmds:
            if cmd.kind == "switch_profile":
                self._handle_switch_profile(cmd.text or cmd.server_id)
                return
            self._send(cmd)
        self._refresh()

    def _handle_switch_profile(self, name: str) -> None:
        if self.config.meta.env_locked_profile or self._switch_profile is None:
            self._refresh()
            self._set_status_panel("profile locked", [self.config.meta.active_profile], "amber")
            return
        try:
            new_config = self._switch_profile(name)
        except ConfigError as exc:
            self._refresh()
            self._set_status_panel("profile failed", [str(exc)[:60]], "amber")
            return
        if new_config is None:
            self._refresh()
            self._set_status_panel("profile locked", [self.config.meta.active_profile], "amber")
            return
        self._apply_config(new_config)

    def _apply_config(self, new_config: Config) -> None:
        # A successful apply supersedes any held "reload failed" notice.
        self._status_panel = None
        old_servers = {server.id for server in self.config.servers}
        self.config = new_config
        if self._runtime_control is not None:
            self._runtime_control.update_config(new_config)
        self.notifier = _build_notifier(new_config)
        self._rebuild_blocked_runtime(new_config)
        self._adopt_usage_config(new_config)
        self.orch.update_config(new_config)
        allowed_servers = {s.id for s in new_config.servers}
        self._blocked_keys = {k for k in self._blocked_keys if k.server_id in allowed_servers}
        restarted = set(self._update_connectors(new_config) or [])
        affected = (old_servers - allowed_servers) | restarted
        for server_id in affected:
            self._servers_up.discard(server_id)
            self._close_server_terminals(server_id)
        if restarted:
            self.orch.clear_server_state(restarted)
            self._blocked_keys = {k for k in self._blocked_keys if k.server_id not in restarted}
        for server_id in restarted:
            self.orch.set_connection(server_id, False)
        self._rearm_interactive_blocked_alerts()
        self._refresh()

    def reload_from_disk(self) -> None:
        if self._config_reloader is None:
            return
        try:
            new_config = self._config_reloader()
        except ConfigError as exc:
            self._refresh()
            self._set_status_panel("reload failed", [str(exc)[:60]], "amber")
            return
        self._apply_config(new_config)


async def _guarded(conn: Connector) -> None:
    try:
        await conn.run()
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


async def _guard(coro) -> None:
    try:
        await coro
    except Exception:
        pass


async def _ticker(app: App, loop, interval: float = TICK_INTERVAL) -> None:
    while True:
        await asyncio.sleep(interval)
        loop.call_soon_threadsafe(app.handle_tick)


def make_profile_switcher(snapshot):
    from .settings import load_settings, resolve_profile, set_active_profile

    def switch(name: str) -> Config | None:
        changed = set_active_profile(snapshot, name)
        if not changed:
            return None
        refreshed = load_settings(snapshot.config_path, snapshot.local_path)
        return resolve_profile(refreshed).config

    return switch


def make_config_reloader(snapshot):
    import tomllib

    from .settings import load_settings, resolve_profile

    def reload_() -> Config:
        # An edit-in-progress can leave the file partially written; wrap the IO/parse
        # errors as ConfigError so reload_from_disk surfaces the "reload failed" panel
        # and keeps the current config instead of letting them escape the watcher callback.
        try:
            refreshed = load_settings(snapshot.config_path, snapshot.local_path)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"could not read config: {exc}") from exc
        return resolve_profile(refreshed).config

    return reload_


def _install_telegram_runtime(
    app: App,
    config: Config,
    runtime_control: RuntimeAgentControl,
    *,
    getenv=get_secret,
    bot_client_factory=TelegramBotClient,
    interactor_factory=TelegramInteractor,
) -> None:
    app.set_runtime_control(runtime_control)

    def build_blocked_runtime(runtime_config: Config) -> BlockedNotificationRuntime:
        runtime_control.update_config(runtime_config)

        def make_interactor(token: str, tg):
            return interactor_factory(
                bot_client_factory(token),
                runtime_control,
                chat_id=tg.chat_id,
                message_thread_id=tg.message_thread_id,
                allowed_user_ids=tg.allowed_user_ids,
                prompt_max_chars=tg.prompt_max_chars,
            )

        return _build_blocked_notification_runtime(
            runtime_config,
            getenv=getenv,
            telegram_interactor_factory=make_interactor,
        )

    app.set_blocked_runtime_factory(build_blocked_runtime)


async def _poll_telegram_once_from_app(
    app: App,
    *,
    timeout: int = 20,
    idle_sleep=asyncio.sleep,
) -> None:
    generation = app.notification_generation
    poller = app.notification_poller
    if poller is None:
        await idle_sleep(1)
        return
    if getattr(poller, "inbound_disabled", False):
        await idle_sleep(60)
        return
    await poller.poll_once(
        timeout=timeout,
        is_current=lambda: app.notification_generation == generation,
    )
    if getattr(poller, "inbound_disabled", False):
        await idle_sleep(60)


def _start_telegram_poll_loop(app: App, *, create_task=asyncio.create_task):
    async def poll_telegram():
        while True:
            try:
                await _poll_telegram_once_from_app(app)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("telegram poll failed", exc_info=True)
                await asyncio.sleep(2)

    return create_task(poll_telegram())


def _mock_config() -> Config:
    """A zero-setup config for the offline simulator (no file/token needed)."""
    from .config import DEFAULT_PROFILES, ServerConfig

    return Config(
        servers=[ServerConfig("mock", "ws://mock", "x")],
        profiles=dict(DEFAULT_PROFILES),
        overview_order=["mock"],
        grid=(5, 3),
    )


def _mock_agents():
    from .model import AgentKey, AgentState, Status

    rows = [
        ("p1", "claude", "macdoktor-crm", "feat/autopilot-auto-close", Status.WORKING),
        ("p2", "codex", "4cyborg", "main", Status.BLOCKED),
        ("p3", "claude", "dtt-app", "feat/vykup-redesign", Status.IDLE),
        ("p4", "codex", "diktator", "main", Status.DONE),
        ("p5", "claude", "herdeck", "feat/web-simulator", Status.WORKING),
    ]
    out = []
    for pane, agent, repo, branch, status in rows:
        s = AgentState(AgentKey("mock", pane), agent, repo, status)
        s.repo, s.branch = repo, branch
        out.append(s)
    return out


async def _run_mock(config: Config, deck: DeckDriver) -> None:
    """Drive the app with synthetic, lively data — no bridge required."""
    from .model import Status

    loop = asyncio.get_running_loop()
    server = config.servers[0].id
    detection = "Do you want to proceed?\n1. Yes\n2. Yes, and don't ask again\n3. No"

    def send(cmd: Command) -> None:
        if cmd.kind == "read":  # answer reads with a sample prompt
            req = app.next_req_for(cmd)
            loop.call_soon_threadsafe(
                app.handle_result, server, req, {"text": detection, "pane_id": cmd.pane_id}
            )

    app = App(config, deck, send, schedule=lambda fn: loop.call_soon_threadsafe(fn))
    app.handle_connection(server, True)
    agents = _mock_agents()
    app.handle_snapshot(server, agents)

    async def cycle():  # flip a status periodically for life
        order = [Status.WORKING, Status.BLOCKED, Status.IDLE, Status.DONE]
        i = 0
        while True:
            await asyncio.sleep(4)
            a = agents[i % len(agents)]
            a.status = (
                order[(order.index(a.status) + 1) % len(order)]
                if a.status in order
                else Status.WORKING
            )
            app.handle_event(server, a)
            i += 1

    tasks = [_guard(_ticker(app, loop)), _guard(cycle())]
    if hasattr(deck, "run_reader"):
        tasks.append(_guard(deck.run_reader()))
    await asyncio.gather(*tasks)


class ConnectorManager:
    def __init__(self, *, make_connector, start_connector):
        self._make_connector = make_connector
        self._start_connector = start_connector
        self.connectors: dict[str, Connector] = {}
        self._fingerprints: dict[str, tuple[str, str]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def update(self, servers: list[ServerConfig]) -> set[str]:
        wanted = {s.id: s for s in servers}
        restarted: set[str] = set()
        for sid in list(self.connectors):
            old = self._fingerprints[sid]
            new = wanted.get(sid)
            if new is None or (new.url, new.token) != old:
                self._stop_connector(sid)
                if new is not None:
                    restarted.add(sid)
        for sid, server in wanted.items():
            fp = (server.url, server.token)
            if sid not in self.connectors:
                conn = self._make_connector(server)
                self.connectors[sid] = conn
                self._fingerprints[sid] = fp
                task = self._start_connector(conn)
                if task is not None:
                    self._tasks[sid] = task
                restarted.add(sid)
        return restarted

    def get(self, server_id: str) -> Connector | None:
        return self.connectors.get(server_id)

    def tasks(self) -> list[asyncio.Task]:
        return list(self._tasks.values())

    def _stop_connector(self, server_id: str) -> None:
        conn = self.connectors.pop(server_id, None)
        if conn is not None:
            conn.stop()
        task = self._tasks.pop(server_id, None)
        if task is not None:
            task.cancel()
        self._fingerprints.pop(server_id, None)

    def stop_all(self) -> None:
        for sid in list(self.connectors):
            self._stop_connector(sid)


async def _run(
    config: Config,
    deck: DeckDriver,
    switch_profile=None,
    tick_interval: float | None = None,
    config_reloader=None,
    config_paths=None,
) -> None:
    if not config.servers:
        raise ConfigError("no servers configured for remote run")
    loop = asyncio.get_running_loop()

    def send(cmd: Command) -> None:
        conn = manager.get(cmd.server_id)
        if conn is not None:
            asyncio.run_coroutine_threadsafe(
                conn.send(command_to_msg(cmd, app.next_req_for(cmd))), loop
            )

    def send_raw(server_id: str, message: dict) -> bool:
        conn = manager.get(server_id)
        if conn is None:
            return False
        asyncio.create_task(conn.send(message))
        return True

    def make_connector(server: ServerConfig) -> Connector:
        return Connector(
            server,
            on_snapshot=lambda sid, st: loop.call_soon_threadsafe(app.handle_snapshot, sid, st),
            on_event=lambda sid, s: loop.call_soon_threadsafe(app.handle_event, sid, s),
            on_connection=lambda sid, up: loop.call_soon_threadsafe(app.handle_connection, sid, up),
            on_result=lambda req, data, sid=server.id: loop.call_soon_threadsafe(
                app.handle_result, sid, req, data
            ),
            on_term=lambda _sid, message, sid=server.id: loop.call_soon_threadsafe(
                app.handle_term, sid, message
            ),
        )

    def start_connector(conn: Connector) -> asyncio.Task:
        return asyncio.create_task(_guarded(conn))

    manager = ConnectorManager(make_connector=make_connector, start_connector=start_connector)

    app = App(
        config,
        deck,
        send,
        schedule=lambda fn: loop.call_soon_threadsafe(fn),
        notifier=_build_notifier(config),
        notify_schedule=lambda coro: asyncio.create_task(coro),
        switch_profile=switch_profile,
        update_connectors=lambda cfg: manager.update(cfg.servers),
        config_reloader=config_reloader,
        send_raw=send_raw,
    )

    async def runtime_send(cmd: Command, req: str) -> None:
        conn = manager.get(cmd.server_id)
        if conn is not None:
            await conn.send(command_to_msg(cmd, req))

    runtime_control = RuntimeAgentControl(
        config,
        send=runtime_send,
        current_agent=app.orch.get_agent,
    )
    _install_telegram_runtime(app, config, runtime_control)
    for server in config.servers:
        app.orch.set_connection(server.id, False)
    app._refresh()

    watcher = None
    if config_reloader is not None and config_paths:
        from .deckapp.watcher import ConfigWatcher

        watcher = ConfigWatcher(config_paths, lambda: loop.call_soon_threadsafe(app.reload_from_disk))
        watcher.start()

    manager.update(config.servers)
    tasks = manager.tasks()
    tasks.append(_start_telegram_poll_loop(app))
    tasks.append(_guard(_ticker(app, loop, tick_interval or config.hardware.tick_interval)))
    if hasattr(deck, "run_reader"):
        tasks.append(_guard(deck.run_reader()))
    if hasattr(deck, "keep_alive_loop"):
        tasks.append(_guard(deck.keep_alive_loop()))
    try:
        await asyncio.gather(*tasks)
    finally:
        if watcher is not None:
            watcher.close()
        manager.stop_all()


def _iface_addr(probe_host: str) -> str | None:
    """The local source address the OS would route to ``probe_host`` (UDP
    connect — no packet is sent). Used to discover the Tailscale / LAN
    interface addresses for the simulator announcement."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((probe_host, 53))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _simulator_urls(host: str, port: int, token: str) -> list[str]:
    """URLs worth printing for the simulator. A wildcard bind is literally
    unroutable (http://0.0.0.0:…) and the README's primary workflow opens the
    page from a phone over Tailscale — so for wildcard binds the Tailscale
    (100.64/10) and default-route addresses are announced too."""
    if host not in ("0.0.0.0", "::"):
        return [f"http://{host}:{port}/?token={token}"]
    urls: list[str] = []
    tailscale = _iface_addr("100.100.100.100")  # MagicDNS resolver -> ts iface
    if tailscale and tailscale.startswith("100."):
        urls.append(f"http://{tailscale}:{port}/?token={token}")
    lan = _iface_addr("1.1.1.1")
    if lan and f"http://{lan}:{port}/?token={token}" not in urls:
        urls.append(f"http://{lan}:{port}/?token={token}")
    urls.append(f"http://127.0.0.1:{port}/?token={token}")
    return urls


def _resolve_deck_kind(config: Config | None, *, getenv=os.environ.get):
    env_kind = getenv("HERDECK_DECK")
    if env_kind:
        return env_kind
    if getenv("HERDECK_FAKE_DECK"):
        return "fake"
    return config.hardware.deck if config and config.hardware.deck else None


def _resolve_socket_path(config: Config | None, *, getenv=os.environ.get) -> str:
    from .bootstrap import resolve_socket_path

    return resolve_socket_path(config, getenv=getenv)


def _resolve_tick_interval(config: Config | None) -> float:
    return config.hardware.tick_interval if config else TICK_INTERVAL


def make_deck(
    kind,
    slots,
    *,
    hardware=None,
    cols=5,
    language="en",
    d200_factory=None,
    elgato_factory=None,
    web_factory=None,
):
    """Build the deck driver. kind None => auto (d200, elgato, else web)."""
    import os

    from .config import HardwareConfig

    hardware = hardware or HardwareConfig()

    def _call_web_factory():
        host = os.environ.get("HERDECK_WEB_BIND") or hardware.web_bind or "127.0.0.1"
        env_port = os.environ.get("HERDECK_WEB_PORT")
        raw_port = env_port if env_port is not None else hardware.web_port
        port = int(raw_port if raw_port is not None else 8800)
        try:
            return web_factory(host=host, port=port, cols=cols)
        except TypeError:
            pass
        try:
            return web_factory(host=host, port=port)
        except TypeError:
            return web_factory()

    if web_factory is None:

        def web_factory(host=None, port=None, cols=5):
            from .driver.web import WebDeck

            try:
                d = WebDeck(
                    slots,
                    host=host,
                    port=port,
                    icons_dir=hardware.icons_dir,
                    cols=cols,
                    language=language,
                )
            except TypeError:
                # injected test doubles may predate the cols/language parameters
                d = WebDeck(slots, host=host, port=port, icons_dir=hardware.icons_dir)
            for url in _simulator_urls(d.host, d.port, d.press_token):
                print(f"herdeck web simulator on {url}")
            return d

    if d200_factory is None:

        def d200_factory():
            from .driver.d200 import D200Driver

            return D200Driver(
                brightness=hardware.brightness,
                debounce=hardware.debounce,
                keep_alive_interval=hardware.keep_alive_interval,
                icons_dir=hardware.icons_dir,
            )

    if elgato_factory is None:

        def elgato_factory():
            from .driver.elgato import ElgatoDriver

            return ElgatoDriver(brightness=hardware.brightness, icons_dir=hardware.icons_dir)

    if kind == "fake":
        return FakeRenderer(slots)
    if kind == "web":
        return _call_web_factory()
    if kind == "d200":
        return d200_factory()
    if kind == "elgato":
        return elgato_factory()
    if kind is not None:
        raise ValueError(f"unsupported deck kind: {kind}")
    # Auto-detect: prefer the D200, then Elgato, else the web simulator.
    for factory in (d200_factory, elgato_factory):
        try:
            return factory()
        except Exception as exc:
            print(f"No Stream Deck opened ({exc}); close any vendor app holding the device.")
    print("Falling back to the web simulator.")
    return _call_web_factory()


async def _amain(
    mode,
    file_config,
    deck,
    *,
    switch_profile=None,
    tick_interval: float | None = None,
    config_reloader=None,
    config_paths=None,
) -> None:
    config, aclose = await resolve_runtime_config(mode, file_config)
    runtime_switch = make_runtime_profile_switcher(
        config,
        switch_profile,
        local_bridge=mode[0] == "local",
    )
    try:
        await _run(
            config,
            deck,
            switch_profile=runtime_switch,
            tick_interval=tick_interval,
            config_reloader=config_reloader if mode[0] == "remote" else None,
            config_paths=config_paths if mode[0] == "remote" else None,
        )
    finally:
        await aclose()


async def _amain_elgato(mode, file_config, socket_path, token) -> None:
    from .elgato.runtime import serve_elgato

    config, aclose = await resolve_runtime_config(mode, file_config)
    try:
        await serve_elgato(config, socket_path=socket_path, token=token)
    finally:
        await aclose()


def main() -> None:
    import os
    import sys

    if os.environ.get("HERDECK_DEBUG"):
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(levelname)s %(message)s",
        )
    mock = bool(os.environ.get("HERDECK_MOCK"))
    config_path = None if mock else _discover_config_path()
    snapshot = None
    switch_profile = None
    config_reloader = None
    config_paths = None
    if config_path:
        from .settings import load_settings, resolve_profile

        local_config_path = _discover_local_config_path(config_path)
        snapshot = load_settings(config_path, local_config_path)
        file_config = resolve_profile(snapshot).config
        switch_profile = make_profile_switcher(snapshot)
        config_reloader = make_config_reloader(snapshot)
        config_paths = [snapshot.config_path, snapshot.local_path]
    else:
        file_config = None
    socket_path = _resolve_socket_path(file_config)
    mode = resolve_mode(
        mock=mock,
        config_path=config_path,
        config_has_servers=bool(file_config and file_config.servers),
        socket_path=socket_path,
        socket_exists=os.path.exists(socket_path),
    )
    if mode[0] == "error":
        print(mode[1], file=sys.stderr)
        sys.exit(2)

    grid = file_config.grid if file_config else (5, 3)
    slots = grid[0] * grid[1] - 2
    kind = _resolve_deck_kind(file_config)
    if kind == "elgato-plugin":
        # The Elgato plugin is its own IPC front-end over the core; it does NOT use
        # the grid Orchestrator/DeckDriver path, so route it before building a deck.
        from .elgato.runtime import discover_ipc

        sock, token = discover_ipc()
        asyncio.run(_amain_elgato(mode, file_config, sock, token))
        return
    deck = make_deck(
        kind,
        slots,
        hardware=file_config.hardware if file_config else None,
        cols=grid[0],
        language=file_config.view.language if file_config else "en",
    )
    try:
        if mode[0] == "mock":
            asyncio.run(_run_mock(_mock_config(), deck))
        else:
            asyncio.run(
                _amain(
                    mode,
                    file_config,
                    deck,
                    switch_profile=switch_profile,
                    tick_interval=_resolve_tick_interval(file_config),
                    config_reloader=config_reloader,
                    config_paths=config_paths,
                )
            )
    finally:
        deck.close()


if __name__ == "__main__":
    main()
