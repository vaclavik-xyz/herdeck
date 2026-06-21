from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable

from .config import Config, ConfigError, load_config
from .connector import Connector
from .driver.base import DeckDriver
from .driver.fake import FakeRenderer
from .model import AgentState, Status
from .notify import (
    NoopNotifier,
    Notifier,
    _macos_sink,
    composite_sink,
    make_telegram_sink,
)
from .orchestrator import Command, Orchestrator

TICK_INTERVAL = 0.4
# Every Nth tick, fully re-render so elapsed-time text on non-working tiles
# (idle/blocked/done) advances even without a status change. 25 * 0.4s ≈ 10s.
FULL_REFRESH_TICKS = 25

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
    getenv=os.environ.get,
    macos_sink=_macos_sink,
    telegram_factory=make_telegram_sink,
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
            tg = n.telegram
            token = getenv(tg.token_env) if tg else None
            if tg and token and tg.chat_id:
                sinks.append(telegram_factory(token, tg.chat_id))
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


class App:
    """Glue between orchestrator (sync) and connectors (async)."""

    def __init__(
        self,
        config: Config,
        deck: DeckDriver,
        send: Callable[[Command], None],
        schedule: Callable[[Callable[[], None]], None] | None = None,
        notifier: Notifier | None = None,
        notify_schedule: Callable[[Callable[[], None]], None] | None = None,
    ):
        self.config = config
        self.deck = deck
        self._send = send
        self._schedule = schedule or (lambda fn: fn())
        self.notifier = notifier or NoopNotifier()
        # Notifications run off the render loop so a slow sink never blocks it;
        # the default runs synchronously (tests, mock) — the lead passes an executor.
        self._notify_schedule = notify_schedule or (lambda fn: fn())
        self._blocked_keys: set = set()
        self.orch = Orchestrator(config, slots=deck.slot_count())
        deck.on_press(self._on_press)
        self._req = 0
        self._active_read_req: str | None = None
        self._ticks = 0

    def next_req_for(self, cmd: Command) -> str | None:
        if cmd.kind == "list":
            return None
        self._req += 1
        req = f"r{self._req}"
        if cmd.kind == "read":
            self._active_read_req = req
        return req

    def _refresh(self) -> None:
        rs = self.orch.render()
        try:
            self.deck.render(rs.tiles)
            self.deck.render_panel(rs.panel)
        except Exception:
            pass  # a render failure must never freeze the loop

    def _invalidate_read(self) -> None:
        self._active_read_req = None
        self.orch.set_detection("")

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
        for s in (x for x in states if x.key in to):
            label = s.repo or s.label
            parts = [p for p in (s.branch, s.key.server_id if multi else None) if p]
            body = f"{label}" + (f" · {' · '.join(parts)}" if parts else "")
            self._schedule_notify(s.agent_type, body)

    def _schedule_notify(self, title: str, body: str) -> None:
        sound = self.config.notifications.sound
        self._notify_schedule(lambda: self.notifier.notify(title, body, sound))

    def handle_snapshot(self, server_id: str, states: list[AgentState]) -> None:
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "snapshot %s: %s",
                server_id,
                [(s.key.pane_id, s.agent_type, s.label, s.status.value) for s in states],
            )
        key = self.orch.drill_key()
        before = self.orch.get_agent(key) if key is not None else None
        self.orch.apply_snapshot(server_id, states)
        if key is not None and key.server_id == server_id:
            if self.orch.get_agent(key) != before:
                self._invalidate_read()
        self._maybe_notify(states, {k for k in self._blocked_keys if k.server_id == server_id})
        self._refresh()

    def handle_event(self, server_id: str, state: AgentState) -> None:
        self.orch.apply_event(server_id, state)
        if self.orch.is_drill_pane(server_id, state.key.pane_id):
            self._invalidate_read()
        self._maybe_notify([state], {state.key})
        self._refresh()

    def handle_connection(self, server_id: str, up: bool) -> None:
        self.orch.set_connection(server_id, up)
        self._refresh()

    def handle_result(self, server_id: str, req: str, data: dict) -> None:
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

    def handle_tick(self) -> None:
        working = self.orch.tick()
        self._ticks += 1
        if self._ticks % FULL_REFRESH_TICKS == 0:
            self._refresh()  # advance elapsed time on all tiles
            return
        if not working:
            return
        if hasattr(self.deck, "render_working"):
            rs = self.orch.render()
            tiles = [t for t in rs.tiles if t.index in set(working)]
            try:
                self.deck.render_working(tiles)
            except Exception:
                pass
        else:
            self._refresh()

    def _on_press(self, index: int) -> None:
        self._schedule(lambda: self._handle_press(index))

    def _handle_press(self, index: int) -> None:
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
            self._send(cmd)
        self._refresh()


def _command_to_msg(cmd: Command, app: App) -> dict:
    if cmd.kind == "list":
        return {"type": "list"}
    req = app.next_req_for(cmd)
    if cmd.kind == "read":
        return {"type": "read", "req": req, "pane_id": cmd.pane_id, "source": cmd.source}
    if cmd.kind == "focus":
        return {"type": "focus", "req": req, "pane_id": cmd.pane_id}
    if cmd.kind == "send_text":
        return {"type": "send_text", "req": req, "pane_id": cmd.pane_id, "text": cmd.text}
    if cmd.kind == "start":
        return {"type": "start", "req": req, "name": cmd.text, "argv": cmd.keys}
    if cmd.kind in ("act_if_blocked", "act_force"):
        return {
            "type": "act",
            "req": req,
            "pane_id": cmd.pane_id,
            "keys": cmd.keys,
            "guard": cmd.kind == "act_if_blocked",
        }
    raise ValueError(f"unknown command kind: {cmd.kind}")


async def _guarded(conn: Connector) -> None:
    try:
        await conn.run()
    except Exception:
        pass


async def _guard(coro) -> None:
    try:
        await coro
    except Exception:
        pass


async def _ticker(app: App, loop) -> None:
    while True:
        await asyncio.sleep(TICK_INTERVAL)
        loop.call_soon_threadsafe(app.handle_tick)


def resolve_mode(*, mock, config_path, config_has_servers, socket_path, socket_exists):
    """Decide how to run from already-gathered facts (pure; no IO)."""
    if mock:
        return ("mock",)
    if config_path is not None and config_has_servers:
        return ("remote", config_path)
    if socket_exists:
        return ("local", socket_path)
    return (
        "error",
        f"No herdr socket at {socket_path} and no [[servers]] config. "
        f"Is herdr running? Set HERDR_SOCKET or create a config "
        f"(see config.example.toml).",
    )


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


async def _run(config: Config, deck: DeckDriver) -> None:
    if not config.servers:
        raise ConfigError("no servers configured for remote run")
    loop = asyncio.get_running_loop()
    connectors: dict[str, Connector] = {}

    def send(cmd: Command) -> None:
        conn = connectors.get(cmd.server_id)
        if conn is not None:
            asyncio.run_coroutine_threadsafe(conn.send(_command_to_msg(cmd, app)), loop)

    app = App(
        config,
        deck,
        send,
        schedule=lambda fn: loop.call_soon_threadsafe(fn),
        notifier=_build_notifier(config),
        notify_schedule=lambda fn: loop.run_in_executor(None, fn),
    )
    for server in config.servers:
        app.orch.set_connection(server.id, False)
    app._refresh()

    for server in config.servers:
        conn = Connector(
            server,
            on_snapshot=lambda sid, st: loop.call_soon_threadsafe(app.handle_snapshot, sid, st),
            on_event=lambda sid, s: loop.call_soon_threadsafe(app.handle_event, sid, s),
            on_connection=lambda sid, up: loop.call_soon_threadsafe(app.handle_connection, sid, up),
            on_result=lambda req, data, sid=server.id: loop.call_soon_threadsafe(
                app.handle_result, sid, req, data
            ),
        )
        connectors[server.id] = conn

    tasks = [_guarded(c) for c in connectors.values()]
    tasks.append(_guard(_ticker(app, loop)))
    if hasattr(deck, "run_reader"):
        tasks.append(_guard(deck.run_reader()))
    if hasattr(deck, "keep_alive_loop"):
        tasks.append(_guard(deck.keep_alive_loop()))
    await asyncio.gather(*tasks)


def make_deck(kind, slots, *, d200_factory=None, elgato_factory=None, web_factory=None):
    """Build the deck driver. kind None => auto (d200, elgato, else web)."""
    import os

    if web_factory is None:

        def web_factory():
            from .driver.web import WebDeck

            host = os.environ.get("HERDECK_WEB_BIND", "127.0.0.1")
            port = int(os.environ.get("HERDECK_WEB_PORT", "8800"))
            d = WebDeck(slots, host=host, port=port)
            print(f"herdeck web simulator on http://{d.host}:{d.port}/?token={d.press_token}")
            return d

    if d200_factory is None:

        def d200_factory():
            from .driver.d200 import D200Driver

            return D200Driver()

    if elgato_factory is None:

        def elgato_factory():
            from .driver.elgato import ElgatoDriver

            return ElgatoDriver()

    if kind == "fake":
        return FakeRenderer(slots)
    if kind == "web":
        return web_factory()
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
    return web_factory()


def local_config(port, token, partial=None):
    """Synthesize the config for local mode from the bound bridge port/token."""
    from .config import (
        DEFAULT_MACROS,
        DEFAULT_PROFILES,
        DEFAULT_START_PROFILES,
        Config,
        Notifications,
        ServerConfig,
    )

    profiles = dict(DEFAULT_PROFILES)
    if partial is not None:
        profiles.update(partial.profiles)
    return Config(
        servers=[ServerConfig("local", f"ws://127.0.0.1:{port}", token)],
        profiles=profiles,
        overview_order=["local"],
        grid=partial.grid if partial else (5, 3),
        macros=partial.macros if partial else list(DEFAULT_MACROS),
        start_profiles=(partial.start_profiles if partial else dict(DEFAULT_START_PROFILES)),
        notifications=partial.notifications if partial else Notifications(),
    )


async def _run_local(socket_path, deck, partial=None):
    from .bridge import start_local_bridge

    host, port, token, _handle = await start_local_bridge(socket_path)
    await _run(local_config(port, token, partial), deck)


def _discover_config_path():
    import os

    p = os.environ.get("HERDECK_CONFIG")
    if p:
        return os.path.abspath(p)
    for cand in (
        os.path.expanduser("~/.config/herdeck/config.toml"),
        os.path.abspath("config.toml"),
    ):
        if os.path.exists(cand):
            return cand
    return None


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
    file_config = load_config(config_path) if config_path else None
    socket_path = os.path.expanduser(os.environ.get("HERDR_SOCKET", "~/.config/herdr/herdr.sock"))
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
    kind = os.environ.get("HERDECK_DECK") or (
        "fake" if os.environ.get("HERDECK_FAKE_DECK") else None
    )
    deck = make_deck(kind, slots)
    try:
        if mode[0] == "mock":
            asyncio.run(_run_mock(_mock_config(), deck))
        elif mode[0] == "remote":
            asyncio.run(_run(file_config, deck))
        else:
            asyncio.run(_run_local(mode[1], deck, file_config))
    finally:
        deck.close()


if __name__ == "__main__":
    main()
