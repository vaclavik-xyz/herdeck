from __future__ import annotations

import asyncio
from collections.abc import Callable

from .config import Config, load_config
from .connector import Connector
from .driver.base import DeckDriver
from .driver.fake import FakeRenderer
from .model import AgentState
from .orchestrator import Command, Orchestrator


class App:
    """Glue between orchestrator (sync) and connectors (async)."""

    def __init__(self, config: Config, deck: DeckDriver,
                 send: Callable[[Command], None],
                 schedule: Callable[[Callable[[], None]], None] | None = None):
        self.config = config
        self.deck = deck
        self._send = send
        self._schedule = schedule or (lambda fn: fn())
        self.orch = Orchestrator(config)
        deck.on_press(self._on_press)

    def _refresh(self) -> None:
        self.deck.render(self.orch.render())

    def handle_snapshot(self, server_id: str, states: list[AgentState]) -> None:
        self.orch.apply_snapshot(server_id, states)
        self._refresh()

    def handle_event(self, server_id: str, state: AgentState) -> None:
        self.orch.apply_event(server_id, state)
        self._refresh()

    def handle_connection(self, server_id: str, up: bool) -> None:
        self.orch.set_connection(server_id, up)
        self._refresh()

    def handle_result(self, server_id: str, data: dict) -> None:
        text = data.get("text")
        if text is not None:                       # a `read` result
            if self.orch.is_drill_pane(server_id, data.get("pane_id")):
                self.orch.set_detection(text)
                self._refresh()
        else:                                      # an `act` result -> resync
            self._send(Command("list", server_id))

    def _on_press(self, index: int) -> None:
        # _on_press may fire on the device thread; marshal the real work
        # onto whatever loop/thread `schedule` targets (the asyncio loop in _run).
        self._schedule(lambda: self._handle_press(index))

    def _handle_press(self, index: int) -> None:
        for cmd in self.orch.on_press(index):
            self._send(cmd)
        self._refresh()


def _command_to_msg(cmd: Command, req_counter: list[int]) -> dict:
    if cmd.kind == "list":
        return {"type": "list"}
    req_counter[0] += 1
    req = f"r{req_counter[0]}"
    if cmd.kind == "read":
        return {"type": "read", "req": req, "pane_id": cmd.pane_id,
                "source": cmd.source}
    if cmd.kind == "act_if_blocked":
        return {"type": "act", "req": req, "pane_id": cmd.pane_id, "keys": cmd.keys}
    raise ValueError(f"unknown command kind: {cmd.kind}")


async def _guarded(conn: Connector) -> None:
    try:
        await conn.run()
    except Exception:
        pass


async def _run(config: Config, deck: DeckDriver) -> None:
    loop = asyncio.get_running_loop()
    connectors: dict[str, Connector] = {}
    req_counter = [0]

    def send(cmd: Command) -> None:
        conn = connectors.get(cmd.server_id)
        if conn is not None:
            asyncio.run_coroutine_threadsafe(
                conn.send(_command_to_msg(cmd, req_counter)), loop)

    app = App(config, deck, send,
              schedule=lambda fn: loop.call_soon_threadsafe(fn))

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
                app.handle_result, sid, data),
        )
        connectors[server.id] = conn

    await asyncio.gather(*(_guarded(c) for c in connectors.values()))


def main() -> None:
    import os

    config = load_config(os.environ.get("HERDECK_CONFIG", "config.toml"))
    if os.environ.get("HERDECK_FAKE_DECK"):
        deck: DeckDriver = FakeRenderer(config.grid[0] * config.grid[1])
    else:
        from .driver.d200 import D200Driver
        deck = D200Driver()
    try:
        asyncio.run(_run(config, deck))
    finally:
        deck.close()
