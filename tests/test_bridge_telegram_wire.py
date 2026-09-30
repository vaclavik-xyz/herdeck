"""Telegram over the bridge WebSocket: capabilities, the ``telegram`` frame,
``telegram_put`` / ``telegram_token`` / ``telegram_test``, and the notifier's
lifecycle inside ``serve`` / ``start_local_bridge``.

Fake Bot API transports only: no real token, no real Telegram API.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time

import pytest
import websockets

from herdeck import bridge as bridge_mod
from herdeck.bridge import StubHerdr, _broadcast, _serve_connection, _wire_panes
from herdeck.bridge_settings import BridgeSettingsStore
from herdeck.bridge_telegram import TELEGRAM_CAPABILITY, TELEGRAM_CONFIG_CAPABILITY
from herdeck.events import EventHub
from herdeck.status_since import StatusSinceTracker
from herdeck.telegram import TelegramBotClient

TOKEN = "123456789:" + "Q" * 35
OTHER = "987654321:" + "Z" * 40
CHAT = "-100123"
ON = {"enabled": True, "chat_id": CHAT}


class Transport:
    """Fake Bot API transport. ``block`` holds getUpdates until set."""

    def __init__(self, *, block: threading.Event | None = None, fail: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.block = block
        self.fail = fail
        self.polling = threading.Event()
        self.poll_threads: list[threading.Thread] = []

    def __call__(self, method: str, fields: dict):
        self.calls.append((method, dict(fields)))
        if method == "getUpdates":
            self.poll_threads.append(threading.current_thread())
            self.polling.set()
            if self.block is not None:
                self.block.wait(10)
            return []
        if method == "sendMessage":
            if self.fail:
                # Telegram-style failure echoing the URL (with the token).
                raise RuntimeError(f"HTTP 500 for https://api.telegram.org/bot{TOKEN}/sendMessage")
            return {"message_id": 1}
        return True

    def sent(self) -> list[dict]:
        return [f for m, f in self.calls if m == "sendMessage"]


class Frames:
    """Every raw frame any test client received (the token-leak check)."""

    def __init__(self):
        self.raw: list[str] = []

    async def recv(self, ws, timeout: float = 3) -> dict:
        raw = await asyncio.wait_for(ws.recv(), timeout)
        self.raw.append(raw)
        return json.loads(raw)

    async def nothing(self, ws, timeout: float = 0.3) -> None:
        with contextlib.suppress(TimeoutError):
            raw = await asyncio.wait_for(ws.recv(), timeout)
            raise AssertionError(f"unexpected frame: {raw}")

    def assert_no_token(self) -> None:
        for raw in self.raw:
            assert TOKEN not in raw and TOKEN.partition(":")[2] not in raw
            assert OTHER not in raw


@pytest.fixture
def frames():
    f = Frames()
    yield f
    f.assert_no_token()


def _build(tmp_path, clients, *, herdr=None, env=None, doc=None, token=False, transport=None,
           debounce_s=0.0):
    herdr = herdr or StubHerdr(panes=[])
    hub = EventHub(herdr, "s")
    settings = BridgeSettingsStore(tmp_path / "s.toml")
    tg = bridge_mod.build_bridge_telegram(
        "s",
        herdr=herdr,
        events=hub,
        settings=settings,
        presence=None,
        clients=clients,
        paths=(tmp_path / "tg.toml", tmp_path / "tg-token"),
        env=env if env is not None else {},
        client_factory=lambda tok: TelegramBotClient(tok, request=transport or Transport()),
        debounce_s=debounce_s,
    )
    if token:
        assert tg.store.set_token(TOKEN) is None
    if doc is not None:
        assert tg.store.put(0, doc, "t").ok
    tg.notifier.refresh()
    return herdr, hub, tg


@contextlib.asynccontextmanager
async def _bridge(tmp_path, **kw):
    clients: dict = {}
    herdr, hub, tg = _build(tmp_path, clients, **kw)

    async def handler(ws):
        await _serve_connection(
            ws, herdr, "s", "tok", clients, "/unused.sock",
            readonly_token="view", events=hub, telegram=tg,
        )

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}", tg
    finally:
        server.close()
        await server.wait_closed()
        await tg.close()
        await hub.close()


async def _connect(url, frames, token="tok"):
    ws = await websockets.connect(url, additional_headers={"Authorization": f"Bearer {token}"})
    snap = await frames.recv(ws)
    assert snap["type"] == "snapshot"
    tg = await frames.recv(ws)
    assert tg["type"] == "telegram"
    return ws, snap, tg


# --- capabilities and the connect frame ------------------------------------------


async def test_config_capability_always_telegram_only_when_active(tmp_path, frames):
    async with _bridge(tmp_path) as (url, _):
        ws, snap, frame = await _connect(url, frames)
        assert TELEGRAM_CONFIG_CAPABILITY in snap["capabilities"]
        assert TELEGRAM_CAPABILITY not in snap["capabilities"]
        assert frame["status"]["active"] is False and frame["settings"] is None
        await ws.close()
    async with _bridge(tmp_path / "b", token=True, doc=ON) as (url, _):
        ws, snap, frame = await _connect(url, frames)
        assert TELEGRAM_CAPABILITY in snap["capabilities"]
        assert frame["status"]["active"] is True
        await ws.close()


async def test_frame_after_first_snapshot_readonly_too(tmp_path, frames):
    async with _bridge(tmp_path, token=True, doc=ON) as (url, _):
        ws, _snap, frame = await _connect(url, frames, "view")
        assert set(frame) == {
            "type", "server_id", "revision", "updated_at_ms", "updated_by", "settings", "status"
        }
        assert frame["server_id"] == "s" and frame["revision"] == 1
        assert frame["settings"]["chat_id"] == CHAT
        assert frame["status"]["token"] == "file"
        await ws.close()


# --- telegram_put -------------------------------------------------------------------


async def test_put_ok_broadcasts_to_all_stale_and_invalid_do_not(tmp_path, frames):
    async with _bridge(tmp_path) as (url, tg):
        a, *_ = await _connect(url, frames)
        b, *_ = await _connect(url, frames, "view")
        await a.send(json.dumps(
            {"type": "telegram_put", "req": "r1", "base_revision": 0, "settings": ON}))
        got = [await frames.recv(a), await frames.recv(a)]
        by_type = {m["type"]: m for m in got}
        assert by_type["result"] == {"type": "result", "req": "r1",
                                     "data": {"ok": True, "revision": 1}}
        assert by_type["telegram"]["revision"] == 1
        assert by_type["telegram"]["settings"]["enabled"] is True
        fb = await frames.recv(b)
        assert fb["type"] == "telegram" and fb["revision"] == 1

        await a.send(json.dumps(
            {"type": "telegram_put", "req": "r2", "base_revision": 0, "settings": ON}))
        res = await frames.recv(a)
        assert res["data"] == {"ok": False, "revision": 1, "error": "stale_revision",
                               "messages": []}
        await a.send(json.dumps(
            {"type": "telegram_put", "req": "r3", "base_revision": 1,
             "settings": {"enabled": "yes"}}))
        res = await frames.recv(a)
        assert res["data"]["ok"] is False and res["data"]["error"] == "invalid"
        await frames.nothing(b)
        assert tg.store.revision == 1
        await a.close()
        await b.close()


async def test_put_write_failure_is_an_error_and_connection_lives(tmp_path, frames, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("herdeck.bridge_telegram.atomic_write_private", boom)
    async with _bridge(tmp_path) as (url, _):
        a, *_ = await _connect(url, frames)
        await a.send(json.dumps(
            {"type": "telegram_put", "req": "r1", "base_revision": 0, "settings": ON}))
        assert await frames.recv(a) == {
            "type": "error", "req": "r1", "message": "telegram write failed"
        }
        await a.send(json.dumps({"type": "health", "req": "h"}))
        assert (await frames.recv(a))["req"] == "h"
        await a.close()


# --- telegram_token -------------------------------------------------------------------


async def test_token_set_valid_then_frame_shows_file_and_no_token(tmp_path, frames, caplog):
    caplog.set_level(logging.DEBUG)
    async with _bridge(tmp_path) as (url, tg):
        a, _snap, first = await _connect(url, frames)
        b, *_ = await _connect(url, frames, "view")
        assert first["status"]["token"] is None
        await a.send(json.dumps(
            {"type": "telegram_token", "req": "t1", "action": "set", "token": TOKEN}))
        got = [await frames.recv(a), await frames.recv(a)]
        by_type = {m["type"]: m for m in got}
        assert by_type["result"] == {"type": "result", "req": "t1", "data": {"ok": True}}
        assert by_type["telegram"]["status"]["token"] == "file"
        assert (await frames.recv(b))["status"]["token"] == "file"
        assert tg.store.token() == TOKEN
        # clear
        await a.send(json.dumps({"type": "telegram_token", "req": "t2", "action": "clear"}))
        got = [await frames.recv(a), await frames.recv(a)]
        by_type = {m["type"]: m for m in got}
        assert by_type["result"]["data"] == {"ok": True}
        assert by_type["telegram"]["status"]["token"] is None
        await a.close()
        await b.close()
    assert TOKEN not in caplog.text


@pytest.mark.parametrize(
    "msg",
    [
        {"action": "set", "token": "not-a-token"},
        {"action": "set"},
        {"action": "set", "token": TOKEN + "\n"},
        {"action": "rotate", "token": TOKEN},
    ],
)
async def test_token_invalid(tmp_path, frames, msg):
    async with _bridge(tmp_path) as (url, tg):
        a, *_ = await _connect(url, frames)
        await a.send(json.dumps({"type": "telegram_token", "req": "t", **msg}))
        res = await frames.recv(a)
        assert res == {"type": "result", "req": "t", "data": {"ok": False, "error": "invalid"}}
        await frames.nothing(a)
        assert tg.store.token() is None
        await a.close()


async def test_token_env_locked(tmp_path, frames):
    async with _bridge(tmp_path, env={"HERDECK_BRIDGE_TELEGRAM_TOKEN": OTHER}) as (url, _):
        a, _snap, first = await _connect(url, frames)
        assert first["status"]["token"] == "env"
        for msg in ({"action": "set", "token": TOKEN}, {"action": "clear"}):
            await a.send(json.dumps({"type": "telegram_token", "req": "t", **msg}))
            res = await frames.recv(a)
            assert res["data"] == {"ok": False, "error": "env_locked"}
        await a.close()


async def test_token_write_failure_is_io_error(tmp_path, frames, monkeypatch):
    async with _bridge(tmp_path) as (url, tg):
        def boom(token):
            raise OSError(f"cannot write {token}")

        monkeypatch.setattr(tg.store, "set_token", boom)
        a, *_ = await _connect(url, frames)
        await a.send(json.dumps(
            {"type": "telegram_token", "req": "t", "action": "set", "token": TOKEN}))
        res = await frames.recv(a)
        assert res["data"] == {"ok": False, "error": "io_error"}
        await a.send(json.dumps({"type": "health", "req": "h"}))
        assert (await frames.recv(a))["req"] == "h"
        await a.close()


# --- readonly -------------------------------------------------------------------------


async def test_readonly_refused_for_all_three(tmp_path, frames):
    transport = Transport()
    async with _bridge(tmp_path, token=True, doc=ON, transport=transport) as (url, tg):
        ws, *_ = await _connect(url, frames, "view")
        for msg in (
            {"type": "telegram_put", "req": "r", "base_revision": 1, "settings": {}},
            {"type": "telegram_token", "req": "r", "action": "clear"},
            {"type": "telegram_test", "req": "r"},
        ):
            await ws.send(json.dumps(msg))
            err = await frames.recv(ws)
            assert err["type"] == "error" and err["req"] == "r"
            assert "read-only" in err["message"]
        assert tg.store.revision == 1 and tg.store.token() == TOKEN
        assert transport.sent() == []
        await ws.close()


# --- telegram_test --------------------------------------------------------------------


async def test_telegram_test_sends_to_fake_transport(tmp_path, frames):
    transport = Transport()
    async with _bridge(tmp_path, token=True, doc={**ON, "enabled": False},
                       transport=transport) as (url, _):
        a, *_ = await _connect(url, frames)
        await a.send(json.dumps({"type": "telegram_test", "req": "x"}))
        res = await frames.recv(a)
        while res["type"] != "result":
            res = await frames.recv(a)
        assert res == {"type": "result", "req": "x", "data": {"ok": True}}
        assert [m["text"] for m in transport.sent()] == ["herdeck ✓ test"]
        assert transport.sent()[0]["chat_id"] == CHAT
        await a.close()


async def test_telegram_test_failure_is_scrubbed(tmp_path, frames, caplog):
    transport = Transport(fail=True)
    async with _bridge(tmp_path, token=True, doc=ON, transport=transport) as (url, _):
        a, *_ = await _connect(url, frames)
        await a.send(json.dumps({"type": "telegram_test", "req": "x"}))
        res = await frames.recv(a)
        while res["type"] != "result":
            res = await frames.recv(a)
        assert res["data"]["ok"] is False and res["data"]["error"]
        await a.close()
    assert TOKEN not in caplog.text


async def test_telegram_test_without_token(tmp_path, frames):
    async with _bridge(tmp_path, doc=ON) as (url, _):
        a, *_ = await _connect(url, frames)
        await a.send(json.dumps({"type": "telegram_test", "req": "x"}))
        res = await frames.recv(a)
        assert res["data"] == {"ok": False, "error": "no bot token"}
        await a.close()


async def test_telegram_test_does_not_block_the_connection(tmp_path, frames):
    """A slow Bot API never stalls deck messages on the same connection."""
    block = threading.Event()

    class Slow(Transport):
        def __call__(self, method, fields):
            if method == "sendMessage":
                block.wait(5)
            return super().__call__(method, fields)

    try:
        async with _bridge(tmp_path, token=True, doc=ON, transport=Slow()) as (url, _):
            a, *_ = await _connect(url, frames)
            await a.send(json.dumps({"type": "telegram_test", "req": "x"}))
            await a.send(json.dumps({"type": "health", "req": "h"}))
            assert (await frames.recv(a))["req"] == "h"
            block.set()
            res = await frames.recv(a)
            while res["type"] != "result":
                res = await frames.recv(a)
            assert res["req"] == "x" and res["data"]["ok"] is True
            await a.close()
    finally:
        block.set()


# --- status changes -------------------------------------------------------------------


async def test_status_change_is_debounced_and_coalesced(tmp_path, frames):
    transport = Transport(fail=True)
    async with _bridge(tmp_path, token=True, doc=ON, transport=transport,
                       debounce_s=0.4) as (url, _):
        a, *_ = await _connect(url, frames)
        b, *_ = await _connect(url, frames, "view")
        loop = asyncio.get_running_loop()
        start = loop.time()
        for req in ("x1", "x2"):  # two failures -> last_error changes
            await a.send(json.dumps({"type": "telegram_test", "req": req}))
        results = 0
        frame = None
        while frame is None:
            m = await frames.recv(a)
            if m["type"] == "result":
                results += 1
            elif m["type"] == "telegram":
                frame = m
        assert loop.time() - start >= 0.35
        assert frame["status"]["last_error"]
        fb = await frames.recv(b)
        assert fb["type"] == "telegram" and fb["status"]["last_error"]
        while results < 2:
            assert (await frames.recv(a))["type"] == "result"
            results += 1
        await frames.nothing(b, 0.6)  # coalesced: one frame for both changes
        await a.close()
        await b.close()


def test_default_debounce_is_at_least_two_seconds():
    assert bridge_mod.TELEGRAM_STATUS_DEBOUNCE_S >= 2.0


async def test_active_flip_resends_snapshot_with_capability(tmp_path, frames):
    clients: dict = {}
    herdr, hub, tg = _build(tmp_path, clients, token=True)
    gate = asyncio.Event()

    async def stream():
        yield []
        await gate.wait()

    btask = asyncio.create_task(_broadcast(stream(), clients, "s", events=hub, telegram=tg))

    async def handler(ws):
        await _serve_connection(
            ws, herdr, "s", "tok", clients, "/unused.sock",
            readonly_token="view", events=hub, telegram=tg,
        )

    server = await websockets.serve(handler, "127.0.0.1", 0)
    url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        await asyncio.sleep(0.05)
        a, snap, _ = await _connect(url, frames)
        b, *_ = await _connect(url, frames, "view")
        assert TELEGRAM_CAPABILITY not in snap["capabilities"]
        await a.send(json.dumps(
            {"type": "telegram_put", "req": "r", "base_revision": 0, "settings": ON}))
        got = [await frames.recv(a) for _ in range(3)]
        assert [m["type"] for m in got] == ["result", "snapshot", "telegram"]
        assert TELEGRAM_CAPABILITY in got[1]["capabilities"]
        assert TELEGRAM_CONFIG_CAPABILITY in got[1]["capabilities"]
        got_b = [await frames.recv(b) for _ in range(2)]
        assert [m["type"] for m in got_b] == ["snapshot", "telegram"]
        assert TELEGRAM_CAPABILITY in got_b[0]["capabilities"]
        await a.close()
        await b.close()
    finally:
        gate.set()
        await btask
        server.close()
        await server.wait_closed()
        await tg.close()
        await hub.close()


# --- notifier wiring ------------------------------------------------------------------


async def test_broadcast_feeds_notifier_and_event_listener(tmp_path):
    """observe_panes after events.observe + EventHub listener: a pane going
    blocked sends one Telegram alert."""
    raw = {
        "pane_id": "w1:p1",
        "workspace_id": "w1",
        "cwd": "/tmp/api",
        "foreground_cwd": "/tmp/api",
        "agent_status": "working",
        "agent": "claude",
        "terminal_id": "t1",
    }
    herdr = StubHerdr(panes=[raw])
    herdr.detection["w1:p1"] = "Allow edit?\n1. Yes\n2. No"
    transport = Transport()
    clients: dict = {}
    _, hub, tg = _build(tmp_path, clients, herdr=herdr, token=True, doc=ON, transport=transport)
    since = StatusSinceTracker(None)
    step = asyncio.Queue()

    async def stream():
        while True:
            await step.get()
            panes = _wire_panes(herdr.panes)
            since.stamp(panes)
            yield panes

    btask = asyncio.create_task(_broadcast(stream(), clients, "s", events=hub, telegram=tg))
    try:
        await step.put(1)
        await asyncio.sleep(0.05)
        assert set(tg.notifier.agents())  # observe_panes reached the notifier
        herdr.panes[0]["agent_status"] = "blocked"
        await step.put(1)
        for _ in range(100):
            if transport.sent():
                break
            await asyncio.sleep(0.02)
        await tg.notifier.flush()
        assert len(transport.sent()) == 1
    finally:
        btask.cancel()
        await asyncio.gather(btask, return_exceptions=True)
        await tg.close()
        await hub.close()


async def test_control_reads_prompt_via_herdr_and_answers_through_guard(tmp_path):
    herdr = StubHerdr(panes=[{
        "pane_id": "w1:p1", "workspace_id": "w1", "cwd": "/tmp/api",
        "foreground_cwd": "/tmp/api", "agent_status": "blocked", "agent": "claude",
        "terminal_id": "t1",
    }])
    herdr.detection["w1:p1"] = "Continue?"
    clients: dict = {}
    _, hub, tg = _build(tmp_path, clients, herdr=herdr)
    try:
        panes = _wire_panes(herdr.panes)
        tg.observe_panes(panes)
        (key,) = tg.notifier.agents()
        assert await tg.control.read_prompt(key) == "Continue?"
        result = await tg.control.send_text(key, "yes")
        assert result.sent is True
        assert herdr.sent[-1] == ("w1:p1", "yes")
    finally:
        await tg.close()
        await hub.close()


async def test_closing_detaches_a_pending_long_poll(tmp_path):
    """A getUpdates thread that cannot be cancelled: close() returns at once
    and the executor thread waiting for it is released too."""
    block = threading.Event()
    transport = Transport(block=block)
    clients: dict = {}
    _, hub, tg = _build(tmp_path, clients, token=True, transport=transport)
    try:
        run = asyncio.create_task(tg.notifier.run())
        await asyncio.wait_for(asyncio.to_thread(transport.polling.wait, 3), 4)
        assert transport.poll_threads[0].daemon  # the HTTP itself runs detached
        started = time.monotonic()
        run.cancel()
        await tg.close()
        await asyncio.gather(run, return_exceptions=True)
        assert time.monotonic() - started < 1.5
    finally:
        block.set()
        await hub.close()


def test_process_exit_is_not_held_by_a_long_poll(tmp_path, monkeypatch):
    """asyncio.run joins the default executor on exit; a pending 10 s fake
    getUpdates must not hold serve()'s shutdown."""
    monkeypatch.setattr(bridge_mod, "EXIT_GRACE_S", 0.01)
    monkeypatch.setenv("HERDECK_BRIDGE_TELEGRAM_TOKEN", TOKEN)
    block = threading.Event()
    transport = Transport(block=block)
    monkeypatch.setattr(
        bridge_mod, "TelegramBotClient", lambda tok: TelegramBotClient(tok, request=transport)
    )
    import herdeck.self_update as su

    seams = []

    def factory(request_exit):
        seams.append(request_exit)
        return su.BridgeUpdater(request_exit=request_exit, probe=lambda: (None, "test"))

    async def main():
        task = asyncio.create_task(bridge_mod.serve(
            "/nonexistent/herdr.sock", "127.0.0.1", 0, "s", "tok", updater_factory=factory
        ))
        for _ in range(200):
            if seams and transport.polling.is_set():
                break
            await asyncio.sleep(0.02)
        assert transport.polling.is_set(), "notifier poller never started"
        seams[0]()
        return await asyncio.wait_for(task, 3)

    started = time.monotonic()
    try:
        assert asyncio.run(main()) is False
        assert time.monotonic() - started < 4.0
    finally:
        block.set()


# --- embedded local bridge -------------------------------------------------------------


async def test_embedded_bridges_use_per_session_paths_and_ignore_env(monkeypatch, frames):
    monkeypatch.setenv("HERDECK_BRIDGE_TELEGRAM_TOKEN", TOKEN)
    seen = []
    built = []
    orig_paths = bridge_mod._telegram_default_paths
    orig_build = bridge_mod.build_bridge_telegram
    monkeypatch.setattr(
        bridge_mod, "_telegram_default_paths",
        lambda s=None: (seen.append(s), orig_paths(s))[1],
    )
    monkeypatch.setattr(
        bridge_mod, "build_bridge_telegram",
        lambda *a, **k: (built.append(orig_build(*a, **k)), built[-1])[1],
    )
    handles = []
    for name in ("alpha", "beta"):
        _host, port, token, handle = await bridge_mod.start_local_bridge(
            "unused.sock", herdr=StubHerdr(panes=[]), session=name
        )
        handles.append(handle)
    ws = await websockets.connect(
        f"ws://127.0.0.1:{port}", additional_headers={"Authorization": f"Bearer {token}"}
    )
    snap = await frames.recv(ws)
    assert TELEGRAM_CONFIG_CAPABILITY in snap["capabilities"]
    kinds = {}
    for _ in range(2):
        m = await frames.recv(ws)
        kinds[m["type"]] = m
    assert kinds["telegram"]["server_id"] == "local"
    assert kinds["telegram"]["status"]["token"] is None  # env is the runtime's, not ours
    await ws.close()
    await asyncio.sleep(0.05)
    assert all(t.notifier_running() for t in built)
    for server, btask in handles:
        btask.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await btask
        server.close()
        await server.wait_closed()
    assert seen == ["alpha", "beta"]
    assert all(t.closed for t in built)
    assert orig_paths("alpha") != orig_paths("beta")


# --- capability flip vs snapshot broadcasts (serialized) ------------------------------


class FakeWs:
    """A client whose sends are recorded (in the order they reach it)."""

    def __init__(self):
        self.got: list[dict] = []

    async def send(self, msg: str) -> None:
        assert TOKEN not in msg
        self.got.append(json.loads(msg))

    async def close(self, **kw) -> None:
        pass

    def snapshots(self) -> list[dict]:
        return [m for m in self.got if m["type"] == "snapshot"]


def _flip(tg) -> asyncio.Task:
    """Enable Telegram (active flips on) and start its immediate broadcast,
    as a telegram_put handler would."""
    assert tg.store.put(tg.store.revision, ON, "t").ok
    tg.refresh()
    return asyncio.get_running_loop().create_task(tg.broadcast())


def _pane(pid: str) -> dict:
    p = {"pane_id": pid, "status": "idle", "agent_type": "claude", "label": pid,
         "workspace": "w", "terminal_id": "t-" + pid}
    EventHub.stamp([p])
    return p


async def _flip_during_broadcast(tmp_path, *, flip_first: bool):
    """A real snapshot broadcast of P2 races a flip's snapshot resend.

    The client's send lock is held by the test, so both sends queue on it.
    ``flip_first``: the flip's broadcast task is created while _broadcast
    computes its capabilities, i.e. it reaches the client lock FIRST;
    otherwise the flip happens while P2's send is already queued."""
    ws, client_lock = FakeWs(), asyncio.Lock()
    clients = {ws: client_lock}
    _, hub, tg = _build(tmp_path, clients, token=True)
    p1, p2 = [_pane("w:p1")], [_pane("w:p2")]
    steps: asyncio.Queue = asyncio.Queue()
    flips: list[asyncio.Task] = []

    async def stream():
        while True:
            yield await steps.get()

    if flip_first:
        original = tg.capabilities

        def hooked():
            caps = original()
            if len(flips) == 0 and tg._panes is not None and steps.empty():
                flips.append(_flip(tg))
            return caps

        tg.capabilities = hooked

    btask = asyncio.create_task(_broadcast(stream(), clients, "s", events=hub, telegram=tg))
    try:
        await steps.put(p1)
        for _ in range(50):
            if tg._panes is not None:
                break
            await asyncio.sleep(0.01)
        await client_lock.acquire()
        await steps.put(p2)
        await asyncio.sleep(0.05)
        if not flip_first:
            flips.append(_flip(tg))
            await asyncio.sleep(0.05)
        assert flips, "flip never happened"
        client_lock.release()
        await asyncio.wait_for(flips[0], 3)
        for _ in range(50):
            if len(ws.snapshots()) >= 3:
                break
            await asyncio.sleep(0.01)
        return ws
    finally:
        btask.cancel()
        await asyncio.gather(btask, return_exceptions=True)
        await tg.close()
        await hub.close()


@pytest.mark.parametrize("flip_first", [False, True])
async def test_flip_resend_never_stale_or_capability_less(tmp_path, flip_first):
    ws = await _flip_during_broadcast(tmp_path, flip_first=flip_first)
    snaps = ws.snapshots()
    last = snaps[-1]
    assert [p["pane_id"] for p in last["panes"]] == ["w:p2"], "stale pane list went out last"
    assert TELEGRAM_CAPABILITY in last["capabilities"], "latest snapshot lacks telegram"
    # The P2 list never follows... and no older list after a newer one.
    order = [p["pane_id"] for s in snaps for p in s["panes"]]
    assert order.index("w:p2") > order.index("w:p1")
    assert "w:p1" not in order[order.index("w:p2"):]
    assert ws.got[-1]["type"] == "telegram" and ws.got[-1]["status"]["active"] is True


async def test_flip_while_connecting_reaches_the_new_client(tmp_path):
    """A flip between the connect snapshot's capabilities and the client's
    registration must still reach that client."""

    class ConnWs(FakeWs):
        def __init__(self):
            super().__init__()
            self.request = type("R", (), {"headers": {"Authorization": "Bearer tok"}})()
            self.done = asyncio.Event()

        async def send(self, msg: str) -> None:
            if not self.got:
                await asyncio.sleep(0.05)  # the first snapshot's send yields
            await super().send(msg)

        def __aiter__(self):
            return self

        async def __anext__(self):
            await self.done.wait()
            raise StopAsyncIteration

    clients: dict = {}
    herdr, hub, tg = _build(tmp_path, clients, token=True)
    tg.observe_panes([])  # a broadcast list exists
    flips: list[asyncio.Task] = []
    original = tg.capabilities

    def hooked():
        caps = original()
        if not flips:
            flips.append(_flip(tg))  # right after the connect snapshot's caps
        return caps

    tg.capabilities = hooked
    ws = ConnWs()
    conn = asyncio.create_task(_serve_connection(
        ws, herdr, "s", "tok", clients, "/unused.sock", events=hub, telegram=tg,
    ))
    try:
        for _ in range(100):
            if flips and flips[0].done() and ws in clients:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        snaps = ws.snapshots()
        assert snaps and TELEGRAM_CAPABILITY in snaps[-1]["capabilities"]
        assert [m for m in ws.got if m["type"] == "telegram"][-1]["status"]["active"] is True
    finally:
        ws.done.set()
        await asyncio.wait_for(conn, 3)
        await tg.close()
        await hub.close()


async def test_hub_closes_even_if_telegram_close_fails(monkeypatch):
    built = []
    orig_build = bridge_mod.build_bridge_telegram

    def build(*a, **k):
        tg = orig_build(*a, **k)

        async def boom():
            raise RuntimeError("close failed")

        tg.close = boom
        built.append(tg)
        return tg

    monkeypatch.setattr(bridge_mod, "build_bridge_telegram", build)
    closed = []
    orig_close = EventHub.close

    async def hub_close(self):
        closed.append(self)
        await orig_close(self)

    monkeypatch.setattr(EventHub, "close", hub_close)
    *_, (server, btask) = await bridge_mod.start_local_bridge(
        "unused.sock", herdr=StubHerdr(panes=[]), session="x"
    )
    await asyncio.sleep(0.05)
    btask.cancel()
    with contextlib.suppress(asyncio.CancelledError, RuntimeError):
        await btask
    server.close()
    await server.wait_closed()
    assert closed, "hub.close() skipped after telegram.close() raised"


@pytest.mark.parametrize(
    ("env", "file_token", "source"),
    [({"HERDECK_BRIDGE_TELEGRAM_TOKEN": OTHER}, False, "env"), ({}, True, "file"), ({}, False, "none")],
)
def test_startup_logs_token_source_never_value(tmp_path, caplog, env, file_token, source):
    caplog.set_level("INFO", logger="herdeck.bridge")
    if file_token:
        (tmp_path / "tg-token").write_text(TOKEN)
    _build(tmp_path, {}, env=env)
    lines = [r.getMessage() for r in caplog.records
             if r.name == "herdeck.bridge" and "token source" in r.getMessage()]
    assert len(lines) == 1 and f"token source: {source}" in lines[0]
    assert ("tg-token" in lines[0]) == (source == "file")  # no path when unused
    for secret in (TOKEN, OTHER):
        assert secret not in caplog.text and secret.partition(":")[2] not in caplog.text
