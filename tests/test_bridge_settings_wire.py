import asyncio
import contextlib
import json

import websockets

from herdeck.bridge import StubHerdr, _serve_connection
from herdeck.bridge_settings import SETTINGS_CAPABILITY, BridgeSettingsStore

GOOD = {"notifications": {"done_min_work": 3}, "macros": [{"label": "go", "text": "continue"}]}


@contextlib.asynccontextmanager
async def _bridge(settings):
    clients: dict = {}

    async def handler(ws):
        await _serve_connection(
            ws, StubHerdr(panes=[]), "s", "tok", clients, "/unused.sock",
            readonly_token="view", settings=settings,
        )

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


async def _recv(ws, timeout=3):
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def _connect(url, token="tok"):
    ws = await websockets.connect(url, additional_headers={"Authorization": f"Bearer {token}"})
    snap = await _recv(ws)
    return ws, snap


async def test_connect_sends_capability_and_unset_frame(tmp_path):
    store = BridgeSettingsStore(tmp_path / "s.toml")
    async with _bridge(store) as url:
        ws, snap = await _connect(url)
        assert snap["type"] == "snapshot" and SETTINGS_CAPABILITY in snap["capabilities"]
        frame = await _recv(ws)
        assert frame["type"] == "settings" and frame["revision"] == 0
        assert frame["settings"] is None and frame["server_id"] == "s"
        await ws.close()


async def test_readonly_gets_frame_but_cannot_put(tmp_path):
    store = BridgeSettingsStore(tmp_path / "s.toml")
    async with _bridge(store) as url:
        ws, _ = await _connect(url, "view")
        assert (await _recv(ws))["type"] == "settings"
        await ws.send(json.dumps(
            {"type": "settings_put", "req": "r", "base_revision": 0, "settings": GOOD}))
        err = await _recv(ws)
        assert err["type"] == "error" and err["req"] == "r" and "read-only" in err["message"]
        assert store.revision == 0
        await ws.close()


async def test_put_broadcasts_to_all_and_stale_does_not(tmp_path):
    store = BridgeSettingsStore(tmp_path / "s.toml")
    async with _bridge(store) as url:
        a, _ = await _connect(url)
        await _recv(a)
        b, _ = await _connect(url, "view")
        await _recv(b)
        await a.send(json.dumps(
            {"type": "settings_put", "req": "r1", "base_revision": 0, "settings": GOOD}))
        got = [await _recv(a), await _recv(a)]
        by_type = {m["type"]: m for m in got}
        assert by_type["result"]["req"] == "r1"
        assert by_type["result"]["data"] == {"ok": True, "revision": 1}
        assert by_type["settings"]["revision"] == 1
        fb = await _recv(b)
        assert fb["type"] == "settings" and fb["revision"] == 1
        await a.send(json.dumps(
            {"type": "settings_put", "req": "r2", "base_revision": 0, "settings": GOOD}))
        res = await _recv(a)
        assert res["data"]["ok"] is False and res["data"]["error"] == "stale_revision"
        assert res["data"]["revision"] == 1
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(b.recv(), 0.3)
            raise AssertionError("stale put must not broadcast")
        await a.close()
        await b.close()


async def test_write_failure_replies_error_and_keeps_serving(tmp_path, monkeypatch):
    store = BridgeSettingsStore(tmp_path / "s.toml")

    def boom(doc):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write", boom)
    async with _bridge(store) as url:
        a, _ = await _connect(url)
        await _recv(a)
        await a.send(json.dumps(
            {"type": "settings_put", "req": "r1", "base_revision": 0, "settings": GOOD}))
        err = await _recv(a)
        assert err == {"type": "error", "req": "r1", "message": "settings write failed"}
        assert store.revision == 0
        await a.send(json.dumps({"type": "health", "req": "h"}))
        assert (await _recv(a))["req"] == "h"  # connection still alive
        await a.close()


async def test_no_store_no_capability_no_frame():
    async with _bridge(None) as url:
        ws, snap = await _connect(url)
        assert SETTINGS_CAPABILITY not in snap["capabilities"]
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(ws.recv(), 0.3)
            raise AssertionError("no settings frame expected")
        await ws.close()


async def test_embedded_bridges_get_distinct_settings_paths(monkeypatch):
    import contextlib as cl

    from herdeck import bridge as bridge_mod

    seen = []
    orig = bridge_mod._settings_default_path
    monkeypatch.setattr(
        bridge_mod, "_settings_default_path", lambda s=None: (seen.append(s), orig(s))[1]
    )
    handles = []
    for name in ("alpha", "beta"):
        *_, handle = await bridge_mod.start_local_bridge(
            "unused.sock", herdr=StubHerdr(panes=[]), session=name
        )
        handles.append(handle)
    for server, btask in handles:
        btask.cancel()
        with cl.suppress(asyncio.CancelledError):
            await btask
        server.close()
        await server.wait_closed()
    assert seen == ["alpha", "beta"]
    from herdeck.bridge_settings import default_path

    assert default_path("alpha") != default_path("beta")


async def test_unencodable_put_is_invalid_and_connection_stays_open(tmp_path):
    store = BridgeSettingsStore(tmp_path / "s.toml")
    async with _bridge(store) as url:
        a, _ = await _connect(url)
        await _recv(a)
        bad = {"macros": [{"label": "go", "text": "\ud800"}]}
        await a.send(json.dumps(
            {"type": "settings_put", "req": "r1", "base_revision": 0, "settings": bad}))
        res = await _recv(a)
        assert res["type"] == "result" and res["req"] == "r1"
        assert res["data"]["ok"] is False and res["data"]["error"] == "invalid"
        assert store.revision == 0 and not (tmp_path / "s.toml").exists()
        await a.send(json.dumps({"type": "health", "req": "h"}))
        assert (await _recv(a))["req"] == "h"  # connection still alive
        await a.close()
