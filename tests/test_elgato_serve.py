"""serve_elgato end to end: a fake bridge connector on one side, the plugin's
IPC socket on the other.

The connector is scripted by the test (snapshot, command result, disconnect);
the plugin side is a real Unix-socket client speaking the line protocol, so
each step is asserted on what the Stream Deck would actually receive and on
what the runtime sends back to the bridge.
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import tempfile

import pytest

from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig
from herdeck.elgato import runtime
from herdeck.elgato.protocol import PROTOCOL_VERSION, decode, encode
from herdeck.elgato.session import ElgatoSession
from herdeck.model import AgentKey, AgentState, Status

TOKEN = "plugin-token"
# A plain yes/no prompt: numbered options would need a pick, not Approve.
PROMPT = "Overwrite config.toml? (y/n)"


class FakeIcons:
    """A key's 'image' is its visible content, so pushes can be asserted."""

    def render_tile_bytes(self, tile) -> bytes:
        return f"{tile.label}|{tile.color}|{tile.status_text}".encode()


class FakeConnector:
    """Stands in for herdeck.connector.Connector: records what the runtime
    sends and exposes the callbacks the runtime registered."""

    instances: list[FakeConnector] = []

    def __init__(self, server, **callbacks):
        self.server = server
        self.cb = callbacks
        self.sent: list[dict] = []
        FakeConnector.instances.append(self)

    async def send(self, msg: dict) -> None:
        self.sent.append(msg)

    async def run(self) -> None:
        await asyncio.Event().wait()  # the test drives the callbacks


def _config() -> Config:
    return Config(
        servers=[ServerConfig("dev", "ws://dev", "t")],
        profiles=dict(DEFAULT_PROFILES),
        overview_order=["dev"],
        grid=(5, 3),
    )


async def _next_render(reader, timeout=2.0) -> dict[str, str]:
    """The next render push, as {instanceId: decoded image text}."""
    while True:
        msg = decode(await asyncio.wait_for(reader.readline(), timeout))
        if msg["type"] == "render":
            return {k: base64.b64decode(v["image"]).decode() for k, v in msg["keys"].items()}


async def _until(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


@pytest.fixture
def sock_path():
    # AF_UNIX paths are capped (~104 bytes on macOS): pytest's tmp_path is too long.
    d = tempfile.mkdtemp(prefix="hd-elg-")
    yield os.path.join(d, "s.sock")
    shutil.rmtree(d, ignore_errors=True)


async def test_serve_elgato_drives_the_plugin_from_bridge_callbacks(monkeypatch, sock_path):
    FakeConnector.instances = []
    monkeypatch.setattr(runtime, "create_connector", FakeConnector)
    serve = asyncio.create_task(
        runtime.serve_elgato(
            _config(),
            socket_path=sock_path,
            token=TOKEN,
            make_session=lambda c: ElgatoSession(c, FakeIcons()),
        )
    )
    writer = None
    try:
        await _until(lambda: os.path.exists(sock_path))
        (conn,) = FakeConnector.instances
        reader, writer = await asyncio.open_unix_connection(sock_path)
        writer.write(encode({"type": "hello", "protocol_version": PROTOCOL_VERSION, "token": TOKEN}))
        assert decode(await asyncio.wait_for(reader.readline(), 2))["type"] == "ready"
        writer.write(
            encode(
                {
                    "type": "action_keys",
                    "action_keys": [
                        {"instanceId": "a0", "type": "approve", "coord": {"col": 4, "row": 0}}
                    ],
                }
            )
        )
        writer.write(
            encode({"type": "slots", "slots": [{"instanceId": "s0", "coord": {"col": 0, "row": 0}}]})
        )
        await writer.drain()
        await _until(lambda: True)

        # 1. The bridge comes up and snapshots one blocked agent: the slot shows
        #    it and the runtime proactively reads its prompt.
        agent = AgentState(AgentKey("dev", "p1"), "claude", "api", Status.BLOCKED)
        conn.cb["on_connection"]("dev", True)
        conn.cb["on_snapshot"]("dev", [agent])
        await _until(lambda: any(m["type"] == "read" for m in conn.sent))
        read = next(m for m in conn.sent if m["type"] == "read")
        assert read["pane_id"] == "p1" and read["source"] == "detection"
        await _until(lambda: True)
        seen: dict[str, str] = {}
        while "BLOCKED" not in seen.get("s0", ""):
            seen.update(await _next_render(reader))
        assert seen["s0"].startswith("api|amber|")

        # 2. The read result arrives: the prompt enables Approve.
        conn.cb["on_result"](read["req"], {"pane_id": "p1", "text": PROMPT})
        while "|green|" not in seen.get("a0", ""):
            seen.update(await _next_render(reader))
        assert [m["type"] for m in conn.sent].count("read") == 1  # no re-read once stored

        # An act/focus result (no text) makes the runtime re-list the server.
        conn.cb["on_result"]("r-act", {"ok": True})
        await _until(lambda: {"type": "list"} in conn.sent)

        # 3. The bridge drops: the slot turns offline (red) and Approve disables.
        conn.cb["on_connection"]("dev", False)
        while "|red|" not in seen.get("s0", "") or "|dim|" not in seen.get("a0", ""):
            seen.update(await _next_render(reader))

        writer.write(encode({"type": "bye"}))
        await writer.drain()
    finally:
        # Close the client first: the server's shutdown waits for its connections.
        if writer is not None:
            writer.close()
        serve.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(serve, 2)


async def test_serve_elgato_refuses_to_replace_a_non_socket(tmp_path):
    path = tmp_path / "not-a-socket"
    path.write_text("keep me")
    with pytest.raises(runtime.ConfigError):
        await runtime.serve_elgato(
            _config(),
            socket_path=str(path),
            token=TOKEN,
            make_session=lambda c: ElgatoSession(c, FakeIcons()),
        )
    assert path.read_text() == "keep me"
