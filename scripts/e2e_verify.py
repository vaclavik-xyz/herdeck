"""End-to-end check against a live herdr via the local bridge.

Connects the real Connector + Orchestrator + FakeRenderer to a running
herdeck bridge, waits a few seconds, and prints the resulting deck tiles.
"""
import asyncio

from herdeck.config import AnswerProfile, Config, ServerConfig
from herdeck.connector import Connector
from herdeck.app import App
from herdeck.driver.fake import FakeRenderer

URL = "ws://127.0.0.1:8788"
TOKEN = "testtoken"


def make_config():
    return Config(
        servers=[ServerConfig("dev", URL, TOKEN)],
        profiles={
            "claude": AnswerProfile(["1", "enter"], ["esc"], ["ctrl+c"], ["2", "enter"]),
            "codex": AnswerProfile(["y", "enter"], ["n", "enter"], ["ctrl+c"], ["y", "enter"]),
            "default": AnswerProfile(["enter"], ["esc"], ["ctrl+c"], ["enter"]),
        },
        overview_order=["dev"],
        grid=(5, 3),
    )


async def main():
    cfg = make_config()
    deck = FakeRenderer(13)  # emulate the D200's 13 buttons
    loop = asyncio.get_running_loop()
    connectors = {}

    def send(cmd):
        c = connectors.get(cmd.server_id)
        if c is not None:
            from herdeck.app import _command_to_msg
            asyncio.run_coroutine_threadsafe(c.send(_command_to_msg(cmd, app)), loop)

    app = App(cfg, deck, send, schedule=lambda fn: loop.call_soon_threadsafe(fn))
    conn = Connector(
        cfg.servers[0],
        on_snapshot=lambda sid, st: loop.call_soon_threadsafe(app.handle_snapshot, sid, st),
        on_event=lambda sid, s: loop.call_soon_threadsafe(app.handle_event, sid, s),
        on_connection=lambda sid, up: loop.call_soon_threadsafe(app.handle_connection, sid, up),
        on_result=lambda req, data, sid="dev": loop.call_soon_threadsafe(app.handle_result, sid, req, data),
    )
    connectors["dev"] = conn
    task = asyncio.create_task(conn.run())
    await asyncio.sleep(3.5)
    tiles = list(deck.last)        # capture WHILE connected
    conn.stop()
    try:
        await asyncio.wait_for(task, 2.0)
    except Exception:
        pass

    print("=== deck tiles (non-empty) ===")
    for t in tiles:
        if t.label or t.color not in ("dim",):
            print(f"  [{t.index:2}] {t.color:6} {t.label!r}")


asyncio.run(main())
