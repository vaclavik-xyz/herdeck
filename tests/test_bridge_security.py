"""Bridge hardening: bind-address policy, token rotation, read-only token."""

import asyncio
import contextlib
import json
import os
import stat

import pytest
import websockets

import herdeck.bridge as bridge_mod
from herdeck.bind import validate_bind
from herdeck.bridge import READONLY_MESSAGES, StubHerdr, _serve_connection

# --- (a) HERDECK_BIND policy -------------------------------------------------


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "::1", "localhost", "100.64.0.7", "100.127.255.1", "box.tail1.ts.net"]
)
def test_bind_policy_allows_loopback_and_tailscale(host):
    assert validate_bind(host, env_name="HERDECK_BIND", getenv=lambda *a: "") == host


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "8.8.8.8", "example.com"])
def test_bind_policy_rejects_everything_else_unless_overridden(host):
    with pytest.raises(ValueError, match="HERDECK_BIND must be loopback or a Tailscale"):
        validate_bind(host, env_name="HERDECK_BIND", getenv=lambda *a: "")
    override = {"HERDECK_ALLOW_UNSAFE_BIND": "1"}
    assert validate_bind(host, env_name="HERDECK_BIND", getenv=override.get) == host


def _no_serve(monkeypatch):
    calls = []

    async def fake_serve(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(bridge_mod, "serve", fake_serve)
    monkeypatch.setattr(bridge_mod, "resolve_herdr_socket_path", lambda: "/tmp/h.sock")
    return calls


def test_bridge_main_refuses_an_unsafe_bind(monkeypatch):
    calls = _no_serve(monkeypatch)
    monkeypatch.setenv("HERDECK_BIND", "0.0.0.0")
    monkeypatch.delenv("HERDECK_ALLOW_UNSAFE_BIND", raising=False)
    monkeypatch.setenv("HERDECK_TOKEN", "t")
    with pytest.raises(SystemExit) as info:
        bridge_mod.main([])
    assert "refusing to start" in str(info.value) and "HERDECK_BIND" in str(info.value)
    assert calls == []


def test_bridge_main_serves_on_tailscale_and_with_override(monkeypatch):
    calls = _no_serve(monkeypatch)
    monkeypatch.setenv("HERDECK_TOKEN", "t")
    monkeypatch.delenv("HERDECK_READONLY_TOKEN_FILE", raising=False)
    monkeypatch.setenv("HERDECK_BIND", "100.101.1.2")
    bridge_mod.main([])
    monkeypatch.setenv("HERDECK_BIND", "0.0.0.0")
    monkeypatch.setenv("HERDECK_ALLOW_UNSAFE_BIND", "1")
    bridge_mod.main([])
    assert [args[1] for args, _ in calls] == ["100.101.1.2", "0.0.0.0"]


# --- (b) token rotation ------------------------------------------------------


def test_rotate_token_writes_a_private_file_and_keeps_it_off_stdout(tmp_path, capsys):
    path = tmp_path / "herdeck" / "bridge-token"
    bridge_mod.main(["--rotate-token", "--token-file", str(path)])
    first = path.read_text()
    out = capsys.readouterr().out
    assert len(first) >= 32
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert first not in out
    assert "Restart the bridge" in out and "deck runtime" in out

    bridge_mod.main(["--rotate-token", "--token-file", str(path), "--show"])
    second = path.read_text()
    assert second != first
    assert f"new token: {second}" in capsys.readouterr().out
    assert [p.name for p in path.parent.iterdir()] == ["bridge-token"]  # no temp left


def test_rotate_token_defaults_to_the_configured_token_file(tmp_path, monkeypatch, capsys):
    path = tmp_path / "token"
    path.write_text("old")
    path.chmod(0o600)
    monkeypatch.setenv("HERDECK_TOKEN_FILE", str(path))
    bridge_mod.main(["--rotate-token"])
    assert path.read_text() != "old"
    assert bridge_mod.load_bridge_token() == path.read_text()  # still loadable (0600)


# --- (c) read-only token -----------------------------------------------------


def _token_file(tmp_path, name, value, mode=0o600):
    path = tmp_path / name
    path.write_text(value)
    path.chmod(mode)
    return str(path)


def test_readonly_token_loading(tmp_path):
    assert bridge_mod.load_readonly_token("full", getenv={}.get) is None
    env = {"HERDECK_READONLY_TOKEN_FILE": _token_file(tmp_path, "ro", "view")}
    assert bridge_mod.load_readonly_token("full", getenv=env.get) == "view"
    with pytest.raises(SystemExit, match="must differ"):
        bridge_mod.load_readonly_token("view", getenv=env.get)
    loose = {"HERDECK_READONLY_TOKEN_FILE": _token_file(tmp_path, "loose", "v", 0o644)}
    with pytest.raises(SystemExit, match="0600"):
        bridge_mod.load_readonly_token("full", getenv=loose.get)


def raw_pane():
    return {
        "pane_id": "w1:p1",
        "workspace_id": "w1",
        "cwd": "/tmp/api",
        "foreground_cwd": "/tmp/api",
        "agent_status": "blocked",
        "agent": "claude",
    }


@contextlib.asynccontextmanager
async def _bridge(herdr):
    clients: dict = {}

    async def handler(ws):
        await _serve_connection(
            ws, herdr, "s", "full-token", clients, "/unused.sock", readonly_token="view-token"
        )

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


async def _roundtrip(ws, msg):
    await ws.send(json.dumps(msg))
    return json.loads(await asyncio.wait_for(ws.recv(), 3))


MUTATING = [
    {"type": "act", "req": "1", "pane_id": "w1:p1", "keys": ["y"], "guard": False},
    {"type": "focus", "req": "2", "pane_id": "w1:p1"},
    {"type": "refresh_title", "req": "3", "pane_id": "w1:p1"},
    {"type": "send_text", "req": "4", "pane_id": "w1:p1", "text": "hi"},
    {"type": "choose_if_blocked", "req": "5", "pane_id": "w1:p1", "choice": "1"},
    {"type": "start", "req": "6", "name": "x", "argv": ["claude"]},
    {"type": "something_new", "req": "7"},
]


async def test_readonly_client_gets_snapshots_but_no_mutations():
    herdr = StubHerdr(panes=[raw_pane()])
    herdr.detection["w1:p1"] = "Allow?"
    async with _bridge(herdr) as url:
        headers = {"Authorization": "Bearer view-token"}
        async with websockets.connect(url, additional_headers=headers) as ws:
            greeting = json.loads(await ws.recv())
            assert greeting["type"] == "snapshot" and greeting["panes"]
            assert (await _roundtrip(ws, {"type": "list"}))["type"] == "snapshot"
            read = await _roundtrip(ws, {"type": "read", "req": "r", "pane_id": "w1:p1"})
            assert read["data"]["text"] == "Allow?"
            for msg in MUTATING:
                reply = await _roundtrip(ws, msg)
                assert reply["type"] == "error", msg
                assert reply["req"] == msg["req"]
                assert f"read-only token: '{msg['type']}'" in reply["message"]
    assert herdr.sent == [] and herdr.focused == [] and herdr.started == []
    assert herdr.refreshed_titles == []


async def test_readonly_client_gets_a_health_reply():
    herdr = StubHerdr(panes=[raw_pane()])
    async with _bridge(herdr) as url:
        headers = {"Authorization": "Bearer view-token"}
        async with websockets.connect(url, additional_headers=headers) as ws:
            await ws.recv()  # greeting snapshot
            reply = await _roundtrip(ws, {"type": "health", "req": "h"})
    assert reply["type"] == "result" and reply["req"] == "h"
    assert reply["data"]["herdr_reachable"] is True


async def test_full_token_still_mutates_and_bad_token_is_refused():
    herdr = StubHerdr(panes=[raw_pane()])
    async with _bridge(herdr) as url:
        full = {"Authorization": "Bearer full-token"}
        async with websockets.connect(url, additional_headers=full) as ws:
            await ws.recv()
            reply = await _roundtrip(ws, MUTATING[0])
            assert reply == {"type": "result", "req": "1", "data": {"sent": True}}
        bad = {"Authorization": "Bearer nope"}
        async with websockets.connect(url, additional_headers=bad) as ws:
            with pytest.raises(websockets.ConnectionClosed) as info:
                await asyncio.wait_for(ws.recv(), 3)
            assert info.value.rcvd.code == 4401
    assert herdr.sent == [("w1:p1", ["y"])]


def test_readonly_allowlist_is_exactly_the_documented_set():
    assert READONLY_MESSAGES == {"list", "read", "observe", "observe_stop", "health"}
