from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from .config import Config, ConfigError, load_config
from .connector import Connector
from .driver.base import DeckDriver
from .driver.fake import FakeRenderer
from .model import AgentState
from .orchestrator import Command, Orchestrator

TICK_INTERVAL = 0.4
# Every Nth tick, fully re-render so elapsed-time text on non-working tiles
# (idle/blocked/done) advances even without a status change. 25 * 0.4s ≈ 10s.
FULL_REFRESH_TICKS = 25

log = logging.getLogger("herdeck")


class App:
    """Glue between orchestrator (sync) and connectors (async)."""

    def __init__(self, config: Config, deck: DeckDriver,
                 send: Callable[[Command], None],
                 schedule: Callable[[Callable[[], None]], None] | None = None):
        self.config = config
        self.deck = deck
        self._send = send
        self._schedule = schedule or (lambda fn: fn())
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

    def handle_snapshot(self, server_id: str, states: list[AgentState]) -> None:
        if log.isEnabledFor(logging.DEBUG):
            log.debug("snapshot %s: %s", server_id,
                      [(s.key.pane_id, s.agent_type, s.label, s.status.value)
                       for s in states])
        key = self.orch.drill_key()
        before = self.orch.get_agent(key) if key is not None else None
        self.orch.apply_snapshot(server_id, states)
        if key is not None and key.server_id == server_id:
            if self.orch.get_agent(key) != before:
                self._invalidate_read()
        self._refresh()

    def handle_event(self, server_id: str, state: AgentState) -> None:
        self.orch.apply_event(server_id, state)
        if self.orch.is_drill_pane(server_id, state.key.pane_id):
            self._invalidate_read()
        self._refresh()

    def handle_connection(self, server_id: str, up: bool) -> None:
        self.orch.set_connection(server_id, up)
        self._refresh()

    def handle_result(self, server_id: str, req: str, data: dict) -> None:
        text = data.get("text")
        if text is not None:
            accepted = (req == self._active_read_req
                        and self.orch.is_drill_pane(server_id, data.get("pane_id")))
            log.debug("result read req=%s pane=%s accepted=%s text=%r",
                      req, data.get("pane_id"), accepted, (text or "")[:60])
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
            self._refresh()          # advance elapsed time on all tiles
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
            log.debug("press idx=%s -> cmds=%s | view=%s panel=%r/%s",
                      index, [(c.kind, c.pane_id, c.keys) for c in cmds],
                      labels, rs.panel.title, rs.panel.lines)
        for cmd in cmds:
            self._send(cmd)
        self._refresh()


def _command_to_msg(cmd: Command, app: "App") -> dict:
    if cmd.kind == "list":
        return {"type": "list"}
    req = app.next_req_for(cmd)
    if cmd.kind == "read":
        return {"type": "read", "req": req, "pane_id": cmd.pane_id,
                "source": cmd.source}
    if cmd.kind == "focus":
        return {"type": "focus", "req": req, "pane_id": cmd.pane_id}
    if cmd.kind == "send_text":
        return {"type": "send_text", "req": req, "pane_id": cmd.pane_id,
                "text": cmd.text}
    if cmd.kind == "start":
        return {"type": "start", "req": req, "name": cmd.text, "argv": cmd.keys}
    if cmd.kind in ("act_if_blocked", "act_force"):
        return {"type": "act", "req": req, "pane_id": cmd.pane_id, "keys": cmd.keys,
                "guard": cmd.kind == "act_if_blocked"}
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


async def _ticker(app: "App", loop) -> None:
    while True:
        await asyncio.sleep(TICK_INTERVAL)
        loop.call_soon_threadsafe(app.handle_tick)


def resolve_mode(*, mock, config_path, config_has_servers, socket_path,
                 socket_exists):
    """Decide how to run from already-gathered facts (pure; no IO)."""
    if mock:
        return ("mock",)
    if config_path is not None and config_has_servers:
        return ("remote", config_path)
    if socket_exists:
        return ("local", socket_path)
    return ("error",
            f"No herdr socket at {socket_path} and no [[servers]] config. "
            f"Is herdr running? Set HERDR_SOCKET or create a config "
            f"(see config.example.toml).")


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
    rows = [("p1", "claude", "macdoktor-crm", "feat/autopilot-auto-close", Status.WORKING),
            ("p2", "codex", "4cyborg", "main", Status.BLOCKED),
            ("p3", "claude", "dtt-app", "feat/vykup-redesign", Status.IDLE),
            ("p4", "codex", "diktator", "main", Status.DONE),
            ("p5", "claude", "herdeck", "feat/web-simulator", Status.WORKING)]
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
    detection = ("Do you want to proceed?\n1. Yes\n"
                 "2. Yes, and don't ask again\n3. No")

    def send(cmd: Command) -> None:
        if cmd.kind == "read":               # answer reads with a sample prompt
            req = app.next_req_for(cmd)
            loop.call_soon_threadsafe(app.handle_result, server, req,
                                      {"text": detection, "pane_id": cmd.pane_id})

    app = App(config, deck, send, schedule=lambda fn: loop.call_soon_threadsafe(fn))
    app.handle_connection(server, True)
    agents = _mock_agents()
    app.handle_snapshot(server, agents)

    async def cycle():                       # flip a status periodically for life
        order = [Status.WORKING, Status.BLOCKED, Status.IDLE, Status.DONE]
        i = 0
        while True:
            await asyncio.sleep(4)
            a = agents[i % len(agents)]
            a.status = order[(order.index(a.status) + 1) % len(order)] \
                if a.status in order else Status.WORKING
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
            asyncio.run_coroutine_threadsafe(
                conn.send(_command_to_msg(cmd, app)), loop)

    app = App(config, deck, send, schedule=lambda fn: loop.call_soon_threadsafe(fn))
    for server in config.servers:
        app.orch.set_connection(server.id, False)
    app._refresh()

    for server in config.servers:
        conn = Connector(
            server,
            on_snapshot=lambda sid, st: loop.call_soon_threadsafe(
                app.handle_snapshot, sid, st),
            on_event=lambda sid, s: loop.call_soon_threadsafe(
                app.handle_event, sid, s),
            on_connection=lambda sid, up: loop.call_soon_threadsafe(
                app.handle_connection, sid, up),
            on_result=lambda req, data, sid=server.id: loop.call_soon_threadsafe(
                app.handle_result, sid, req, data),
        )
        connectors[server.id] = conn

    tasks = [_guarded(c) for c in connectors.values()]
    tasks.append(_guard(_ticker(app, loop)))
    if hasattr(deck, "run_reader"):
        tasks.append(_guard(deck.run_reader()))
    if hasattr(deck, "keep_alive_loop"):
        tasks.append(_guard(deck.keep_alive_loop()))
    await asyncio.gather(*tasks)


def make_deck(kind, slots, *, d200_factory=None, web_factory=None):
    """Build the deck driver. kind None => auto (d200, else web fallback)."""
    import os

    if web_factory is None:
        def web_factory():
            from .driver.web import WebDeck
            host = os.environ.get("HERDECK_WEB_BIND", "127.0.0.1")
            port = int(os.environ.get("HERDECK_WEB_PORT", "8800"))
            d = WebDeck(slots, host=host, port=port)
            print(f"herdeck web simulator on http://{d.host}:{d.port}")
            return d

    if d200_factory is None:
        def d200_factory():
            from .driver.d200 import D200Driver
            return D200Driver()

    if kind == "fake":
        return FakeRenderer(slots)
    if kind == "web":
        return web_factory()
    if kind == "d200":
        return d200_factory()
    try:
        return d200_factory()
    except Exception as exc:
        print(f"No Stream Deck opened ({exc}); close Ulanzi Studio if it is "
              f"running. Falling back to the web simulator.")
        return web_factory()


def main() -> None:
    import os

    if os.environ.get("HERDECK_DEBUG"):
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(levelname)s %(message)s",
        )
    mock = bool(os.environ.get("HERDECK_MOCK"))
    if mock:
        config = _mock_config()          # zero-setup: no config file or token needed
    else:
        config_path = os.path.abspath(os.environ.get("HERDECK_CONFIG", "config.toml"))
        config = load_config(config_path)   # load BEFORE the deck chdir's (R-4)
    # HERDECK_DECK selects the driver: d200 (default) | web | fake.
    kind = os.environ.get("HERDECK_DECK") or ("fake" if os.environ.get("HERDECK_FAKE_DECK") else "d200")
    # The last two grid cells are the status panel, not buttons (like the D200).
    slots = config.grid[0] * config.grid[1] - 2
    if kind == "fake":
        deck: DeckDriver = FakeRenderer(slots)
    elif kind == "web":
        from .driver.web import WebDeck
        host = os.environ.get("HERDECK_WEB_BIND", "127.0.0.1")
        port = int(os.environ.get("HERDECK_WEB_PORT", "8800"))
        deck = WebDeck(slots, host=host, port=port)
        print(f"herdeck web simulator on http://{deck.host}:{deck.port}")
    else:
        from .driver.d200 import D200Driver
        deck = D200Driver()
    try:
        asyncio.run(_run_mock(config, deck) if mock else _run(config, deck))
    finally:
        deck.close()


if __name__ == "__main__":
    main()
