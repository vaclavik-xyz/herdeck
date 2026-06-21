"""End-to-end check against a live herdr via the local bridge.

Connects the real Connector + Orchestrator + FakeRenderer to a running
herdeck bridge, waits a few seconds, and prints the resulting deck tiles.
"""

import asyncio
import os

from herdeck.app import App
from herdeck.config import AnswerProfile, Config, ServerConfig
from herdeck.connector import Connector
from herdeck.driver.fake import FakeRenderer

URL = os.environ.get("HERDECK_E2E_URL", "ws://127.0.0.1:8788")
TOKEN = os.environ.get("HERDECK_E2E_TOKEN", "testtoken")


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


def _verify_capture(*, tiles, frames_seen, connected):
    if not tiles:
        return False, "FAIL: no render (connector never produced a frame)"
    if not connected:
        return False, "FAIL: not connected to the bridge"
    if frames_seen == 0:
        return False, "FAIL: connected but the bridge sent no snapshot/event"
    return True, f"OK: connected and rendered ({frames_seen} bridge frames)"


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
    frames = {"n": 0}
    connection = {"up": False}

    def on_snap(sid, st):
        frames["n"] += 1
        loop.call_soon_threadsafe(app.handle_snapshot, sid, st)

    def on_evt(sid, s):
        frames["n"] += 1
        loop.call_soon_threadsafe(app.handle_event, sid, s)

    def on_connection(sid, up):
        connection["up"] = up
        loop.call_soon_threadsafe(app.handle_connection, sid, up)

    conn = Connector(
        cfg.servers[0],
        on_snapshot=on_snap,
        on_event=on_evt,
        on_connection=on_connection,
        on_result=lambda req, data, sid="dev": loop.call_soon_threadsafe(
            app.handle_result, sid, req, data
        ),
    )
    connectors["dev"] = conn
    task = asyncio.create_task(conn.run())
    await asyncio.sleep(3.5)
    tiles = list(deck.last)  # capture WHILE connected
    frames_seen = frames["n"]  # capture frame count at the same instant
    connected = connection["up"]
    conn.stop()
    try:
        await asyncio.wait_for(task, 2.0)
    except Exception:
        pass

    print("=== deck tiles (non-empty) ===")
    for t in tiles:
        if t.label or t.color not in ("dim",):
            print(f"  [{t.index:2}] {t.color:6} {t.label!r}")

    ok, message = _verify_capture(tiles=tiles, frames_seen=frames_seen, connected=connected)
    if not ok:
        print(message)
        return 1
    print(message)
    return 0


raise SystemExit(asyncio.run(main()))
