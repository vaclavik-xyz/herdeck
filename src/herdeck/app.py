from __future__ import annotations

import asyncio
from collections.abc import Callable

from .config import Config, load_config
from .connector import Connector
from .driver.base import DeckDriver
from .driver.fake import FakeRenderer
from .model import AgentState
from .orchestrator import Command, Orchestrator

TICK_INTERVAL = 0.4


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
            if (req == self._active_read_req
                    and self.orch.is_drill_pane(server_id, data.get("pane_id"))):
                self.orch.set_detection(text)
                self._refresh()
        else:
            self._send(Command("list", server_id))

    def handle_tick(self) -> None:
        working = self.orch.tick()
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
        for cmd in self.orch.on_press(index):
            self._send(cmd)
        self._refresh()


def _command_to_msg(cmd: Command, app: "App") -> dict:
    if cmd.kind == "list":
        return {"type": "list"}
    req = app.next_req_for(cmd)
    if cmd.kind == "read":
        return {"type": "read", "req": req, "pane_id": cmd.pane_id,
                "source": cmd.source}
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


async def _run(config: Config, deck: DeckDriver) -> None:
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


def main() -> None:
    import os

    config_path = os.path.abspath(os.environ.get("HERDECK_CONFIG", "config.toml"))
    config = load_config(config_path)   # load BEFORE the deck chdir's (R-4)
    if os.environ.get("HERDECK_FAKE_DECK"):
        deck: DeckDriver = FakeRenderer(config.grid[0] * config.grid[1])
    else:
        from .driver.d200 import D200Driver
        deck = D200Driver()
    try:
        asyncio.run(_run(config, deck))
    finally:
        deck.close()


if __name__ == "__main__":
    main()
