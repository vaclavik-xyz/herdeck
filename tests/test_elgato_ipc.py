import asyncio
import base64

import pytest

from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig
from herdeck.elgato.ipc import IpcServer
from herdeck.elgato.protocol import PROTOCOL_VERSION, decode, encode
from herdeck.elgato.session import ElgatoSession
from herdeck.model import AgentKey, AgentState, Status


class FakeIcons:
    def render_tile_bytes(self, tile) -> bytes:
        return f"{tile.label}|{tile.color}|{tile.repo}".encode()


def make_session():
    cfg = Config(servers=[ServerConfig("dev", "ws://dev", "t")], profiles=dict(DEFAULT_PROFILES),
                 overview_order=["dev"], grid=(5, 3))
    return ElgatoSession(cfg, FakeIcons())


class Pipe:
    """In-memory reader/writer pair good enough for the line protocol."""

    def __init__(self):
        self._buf = asyncio.Queue()
        self.sent = []

    async def readline(self):
        return await self._buf.get()

    def feed(self, line: bytes):
        self._buf.put_nowait(line)

    def write(self, data: bytes):
        self.sent.append(data)

    async def drain(self):
        pass

    def close(self):
        pass


@pytest.mark.asyncio
async def test_hello_with_wrong_token_is_rejected():
    sess = make_session()
    server = IpcServer(sess, token="secret", on_commands=lambda c: None)
    pipe = Pipe()
    pipe.feed(encode({"type": "hello", "protocol_version": PROTOCOL_VERSION, "token": "wrong"}))
    await asyncio.wait_for(server.handle(pipe, pipe), timeout=1)
    types = [decode(b)["type"] for b in pipe.sent]
    assert "ready" not in types
    assert "error" in types


@pytest.mark.asyncio
async def test_hello_with_wrong_protocol_version_is_rejected():
    sess = make_session()
    server = IpcServer(sess, token="secret", on_commands=lambda c: None)
    pipe = Pipe()
    pipe.feed(encode({"type": "hello", "protocol_version": PROTOCOL_VERSION + 1, "token": "secret"}))
    await asyncio.wait_for(server.handle(pipe, pipe), timeout=1)
    types = [decode(b)["type"] for b in pipe.sent]
    assert "ready" not in types and "error" in types


@pytest.mark.asyncio
async def test_hello_with_non_string_token_is_rejected_not_crashed():
    sess = make_session()
    server = IpcServer(sess, token="secret", on_commands=lambda c: None)
    pipe = Pipe()
    pipe.feed(encode({"type": "hello", "protocol_version": PROTOCOL_VERSION, "token": None}))
    await asyncio.wait_for(server.handle(pipe, pipe), timeout=1)
    types = [decode(b)["type"] for b in pipe.sent]
    assert "ready" not in types and "error" in types  # malformed token rejected, not crashed


@pytest.mark.asyncio
async def test_keyup_runs_command_and_pushes_render():
    sess = make_session()
    sess.set_slots([("s0", (0, 0))])
    sess.apply_snapshot("dev", [AgentState(AgentKey("dev", "p1"), "claude", "api", Status.WORKING)])
    got = []
    server = IpcServer(sess, token="secret", on_commands=got.append)
    pipe = Pipe()
    pipe.feed(encode({"type": "hello", "protocol_version": PROTOCOL_VERSION, "token": "secret"}))
    pipe.feed(encode({"type": "slots", "slots": [{"instanceId": "s0", "coord": {"col": 0, "row": 0}}]}))
    pipe.feed(encode({"type": "keyUp", "instanceId": "s0"}))
    pipe.feed(b"")  # EOF
    await asyncio.wait_for(server.handle(pipe, pipe), timeout=1)

    assert got and got[0][0].kind == "focus"
    renders = [decode(b) for b in pipe.sent if decode(b)["type"] == "render"]
    assert renders, "expected a render push after keyUp"
    payload = renders[-1]["keys"]["s0"]
    # keyUp selected p1, so its slot's repo now carries the "* " marker (the field the
    # real renderer draws) — proving the press changed the render, not just the slots msg.
    assert base64.b64decode(payload["image"]) == b"api|green|* api"


@pytest.mark.asyncio
async def test_push_diff_sends_render_on_state_change_without_keypress():
    sess = make_session()
    sess.set_slots([("s0", (0, 0))])
    sess.apply_snapshot("dev", [AgentState(AgentKey("dev", "p1"), "claude", "api", Status.WORKING)])
    server = IpcServer(sess, token="secret", on_commands=lambda c: None)
    pipe = Pipe()
    server._writer = pipe          # simulate an authed connection
    sess.take_render_diff()        # prime the diff baseline
    sess.apply_event("dev", AgentState(AgentKey("dev", "p1"), "claude", "api", Status.BLOCKED))
    await server.push_diff()       # runtime calls this after a live state change
    renders = [decode(b) for b in pipe.sent if decode(b)["type"] == "render"]
    assert renders and "s0" in renders[-1]["keys"]  # pushed with no keyUp


@pytest.mark.asyncio
async def test_push_diff_does_nothing_before_authenticated_hello():
    sess = make_session()
    server = IpcServer(sess, token="secret", on_commands=lambda c: None)
    await server.push_diff()       # nobody has sent a valid hello yet
    assert server._writer is None  # writer is registered only after auth


@pytest.mark.asyncio
async def test_bye_closes_connection_and_clears_writer():
    sess = make_session()
    server = IpcServer(sess, token="secret", on_commands=lambda c: None)
    pipe = Pipe()
    pipe.feed(encode({"type": "hello", "protocol_version": PROTOCOL_VERSION, "token": "secret"}))
    pipe.feed(encode({"type": "bye"}))  # graceful close without an EOF
    await asyncio.wait_for(server.handle(pipe, pipe), timeout=1)
    assert server._writer is None  # bye returned from handle() and cleared the writer
