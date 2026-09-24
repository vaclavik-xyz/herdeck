"""End-to-end check against a live herdr via the local bridge.

Runs the real runtime (LiveSource connectors + DeckApp + Orchestrator) against
a running herdeck bridge with an in-memory deck front, waits a few seconds and
prints the resulting deck tiles.
"""

import os
import time

from herdeck.config import AnswerProfile, Config, ServerConfig
from herdeck.deckapp.live import build_live_source
from herdeck.deckapp.server import DeckApp
from herdeck.deckapp.sinks import DriverSink
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


def main():
    cfg = make_config()
    deck = FakeRenderer(13)  # emulate the D200's 13 buttons
    source = build_live_source(cfg, shell_banners=False)
    app = DeckApp(source, serve=False, clock=time.monotonic)
    app.add_sink(DriverSink(deck, on_press=app.press, slots=app.slots))
    try:
        time.sleep(3.5)
        tiles = list(deck.last)  # capture WHILE connected
        # a server turns "available" once a snapshot arrived on its connection
        frames_seen = int(source.semantic_server_available("dev"))
        connected = source.connected
    finally:
        app.close()

    print("=== deck tiles (non-empty) ===")
    for t in tiles:
        if t.label or t.color not in ("dim",):
            print(f"  [{t.index:2}] {t.color:6} {t.label!r}")

    ok, message = _verify_capture(tiles=tiles, frames_seen=frames_seen, connected=connected)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
