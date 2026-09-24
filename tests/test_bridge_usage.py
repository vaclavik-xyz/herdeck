"""Bridge-side usage (B1): the bridge polls provider usage and pushes `usage`
frames; the wire encoding/decoding; the connector's report; the service flag."""

import asyncio
import contextlib
import json
import plistlib

import pytest
import websockets

import herdeck.bridge as bridge_mod
from herdeck.bridge import BridgeUsageFeed, StubHerdr, _serve_connection, build_bridge_usage
from herdeck.config import ServerConfig
from herdeck.connector import Connector
from herdeck.protocol import Unknown, Usage, decode_inbound
from herdeck.usage import (
    ProviderUsage,
    UsageWindow,
    bridge_usage_config,
    bridge_usage_enabled,
    usage_from_wire,
    usage_to_wire,
)


def _codex(used=40, subscription="paid", early=None):
    return ProviderUsage(
        "codex",
        [UsageWindow("5h", used, "2026-09-24T18:00:00Z", early), UsageWindow("7d", 12, None)],
        subscription,
        "pro",
    )


class FakePoller:
    """Stands in for usage.UsagePoller: no codex, no CodexBar."""

    def __init__(self, data=None):
        self.data = list(data or [])
        self.started = 0
        self.closed = 0
        self.fail = False

    def start(self):
        self.started += 1

    def close(self):
        self.closed += 1

    def snapshot(self):
        if self.fail:
            raise RuntimeError("poller broke")
        return list(self.data)


# --- wire encoding ------------------------------------------------------------


def test_usage_wire_roundtrip_keeps_the_panel_model():
    data = [_codex(early=1800), ProviderUsage("claude", [UsageWindow("5h", 5, None)], "unknown")]
    assert usage_from_wire(json.loads(json.dumps(usage_to_wire(data)))) == data


def test_usage_from_wire_drops_malformed_entries():
    raw = [
        "nope",
        {"provider": "bad id!", "windows": [{"label": "5h", "used_percent": 1}]},
        {"provider": "codex", "windows": []},
        {"provider": "codex", "windows": [{"label": "5h", "used_percent": "40"}]},
        {
            "provider": "claude",
            "subscription": "platinum",
            "plan": 7,
            "windows": [
                {"label": "5h", "used_percent": 250, "resets_at": "garbage", "full_early_s": -5},
                {"label": "x" * 500, "used_percent": -3},
            ],
        },
        {"provider": "claude", "windows": [{"label": "7d", "used_percent": 1}]},  # repeat
    ]
    [claude] = usage_from_wire(raw)
    assert claude.provider == "claude"
    assert claude.subscription == "unknown" and claude.plan is None
    assert claude.windows[0] == UsageWindow("5h", 100, None, None)
    assert claude.windows[1].used_percent == 0 and len(claude.windows[1].label) == 64
    assert usage_from_wire({"not": "a list"}) == []


def test_decode_inbound_usage_frame():
    frame = json.dumps({"type": "usage", "server_id": "b", "providers": usage_to_wire([_codex()])})
    msg = decode_inbound(frame)
    assert isinstance(msg, Usage) and msg.server_id == "b" and msg.providers == [_codex()]
    with pytest.raises(ValueError):
        decode_inbound(json.dumps({"type": "usage", "providers": []}))


# --- bridge config ------------------------------------------------------------


def test_bridge_usage_is_off_unless_enabled():
    assert not bridge_usage_enabled({}.get)
    assert not bridge_usage_enabled({"HERDECK_BRIDGE_USAGE": "0"}.get)
    assert bridge_usage_enabled({"HERDECK_BRIDGE_USAGE": "1"}.get)
    assert bridge_usage_enabled({"HERDECK_USAGE": "true"}.get)  # spec alias
    assert build_bridge_usage("s", getenv={}.get) is None


def test_bridge_usage_config_defaults_and_file(tmp_path):
    cfg = bridge_usage_config({}.get)
    assert cfg.providers == ["codex", "claude"]
    path = tmp_path / "config.toml"
    path.write_text(
        '[usage]\nproviders = ["claude"]\npaid_only = true\nrefresh_secs = 60\n'
        'codex_path = "/opt/codex"\nalert_at = [80]\nsource = "local"\n'
    )
    cfg = bridge_usage_config({"HERDECK_USAGE_CONFIG": str(path)}.get)
    assert cfg.providers == ["claude"] and cfg.refresh_secs == 60
    assert cfg.codex_path == "/opt/codex"
    # Filtering and alerts are each runtime's job, never the bridge's.
    assert cfg.paid_only is False and cfg.alert_at == [] and cfg.alert_reset is False
    path.write_text("[server]\nx = 1\n")  # no [usage] table -> defaults
    assert bridge_usage_config({"HERDECK_USAGE_CONFIG": str(path)}.get).providers == [
        "codex",
        "claude",
    ]


def test_bridge_usage_config_rejects_a_bad_file(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("[usage]\nrefresh_secs = 1\n")
    with pytest.raises(SystemExit, match="refresh_secs"):
        bridge_usage_config({"HERDECK_USAGE_CONFIG": str(path)}.get)
    with pytest.raises(SystemExit, match="HERDECK_USAGE_CONFIG"):
        bridge_usage_config({"HERDECK_USAGE_CONFIG": str(tmp_path / "missing.toml")}.get)


def test_build_bridge_usage_uses_the_real_poller_unstarted(monkeypatch):
    import herdeck.usage as usage_mod

    seen = []
    monkeypatch.setattr(
        usage_mod, "poller_from_config", lambda cfg, on_alert=None: seen.append(cfg) or FakePoller()
    )
    feed = build_bridge_usage("s", getenv={"HERDECK_BRIDGE_USAGE": "1"}.get)
    assert isinstance(feed, BridgeUsageFeed) and feed.capabilities == ("usage",)
    assert seen[0].providers == ["codex", "claude"] and seen[0].paid_only is False


# --- bridge feed lifecycle ----------------------------------------------------


class _WS:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(json.loads(msg))

    async def close(self, **kw):
        pass


async def test_feed_pushes_only_changes_to_all_clients(monkeypatch):
    poller = FakePoller([_codex(40)])
    feed = BridgeUsageFeed(poller, "s", check_s=0.01)
    a, b = _WS(), _WS()
    clients = {a: asyncio.Lock(), b: asyncio.Lock()}
    task = asyncio.create_task(feed.run(clients))
    try:
        await asyncio.sleep(0.05)
        assert len(a.sent) == len(b.sent) == 1  # unchanged snapshots are not resent
        poller.data = [_codex(55)]
        await asyncio.sleep(0.05)
        assert [m["providers"][0]["windows"][0]["used_percent"] for m in a.sent] == [40, 55]
        poller.fail = True  # a poller bug neither ends the feed nor resends junk
        await asyncio.sleep(0.05)
        assert len(a.sent) == 2 and not task.done()
        poller.fail = False
        poller.data = []
        await asyncio.sleep(0.05)
        assert a.sent[-1] == {"type": "usage", "server_id": "s", "providers": []}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_feed_start_close_drive_the_poller():
    poller = FakePoller()
    feed = BridgeUsageFeed(poller, "s")
    feed.start()
    feed.close()
    assert (poller.started, poller.closed) == (1, 1)


async def test_serve_starts_and_closes_the_bridge_poller(monkeypatch, tmp_path):
    import herdeck.self_update as su

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(bridge_mod, "EXIT_GRACE_S", 0.01)
    poller = FakePoller([_codex()])
    seams = []

    def factory(request_exit):
        seams.append(request_exit)
        return su.BridgeUpdater(request_exit=request_exit, probe=lambda: (None, "test"))

    task = asyncio.create_task(
        bridge_mod.serve(
            "/nonexistent/herdr.sock",
            "127.0.0.1",
            0,
            "s",
            "tok",
            updater_factory=factory,
            usage=BridgeUsageFeed(poller, "s"),
        )
    )
    for _ in range(100):
        if seams:
            break
        await asyncio.sleep(0.02)
    assert poller.started == 1 and poller.closed == 0
    seams[0]()
    await asyncio.wait_for(task, 3)
    assert poller.closed == 1  # a bridge restart stops codex app-server


def test_main_hands_the_env_enabled_feed_to_serve(monkeypatch):
    import herdeck.usage as usage_mod

    seen = {}

    async def fake_serve(*args, **kwargs):
        seen.update(kwargs)
        return False

    monkeypatch.setattr(bridge_mod, "serve", fake_serve)
    monkeypatch.setattr(bridge_mod, "resolve_herdr_socket_path", lambda: "/tmp/h.sock")
    monkeypatch.setattr(usage_mod, "poller_from_config", lambda cfg, on_alert=None: FakePoller())
    monkeypatch.setenv("HERDECK_TOKEN", "t")
    monkeypatch.delenv("HERDECK_READONLY_TOKEN_FILE", raising=False)
    monkeypatch.delenv("HERDECK_TOKEN_FILE", raising=False)
    monkeypatch.delenv("HERDECK_USAGE_CONFIG", raising=False)
    monkeypatch.setenv("HERDECK_BIND", "127.0.0.1")
    monkeypatch.setenv("HERDECK_BRIDGE_USAGE", "1")
    bridge_mod.main([])
    assert isinstance(seen["usage"], BridgeUsageFeed)


@contextlib.asynccontextmanager
async def _bridge(herdr, usage):
    clients: dict = {}

    async def handler(ws):
        await _serve_connection(
            ws, herdr, "s", "tok", clients, "/unused.sock", usage=usage, readonly_token="view"
        )

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}", server
    finally:
        server.close()
        await server.wait_closed()


async def test_connect_gets_capability_then_current_usage():
    feed = BridgeUsageFeed(FakePoller([_codex()]), "s")
    async with _bridge(StubHerdr(panes=[]), feed) as (url, _server):
        # A view-only client sees usage too (it is fleet data, not control).
        async with websockets.connect(url, additional_headers={"Authorization": "Bearer view"}) as ws:
            snapshot = json.loads(await asyncio.wait_for(ws.recv(), 3))
            assert "usage" in snapshot["capabilities"]
            usage = json.loads(await asyncio.wait_for(ws.recv(), 3))
            assert usage["type"] == "usage" and usage["providers"][0]["provider"] == "codex"
            await ws.send(json.dumps({"type": "list"}))
            again = json.loads(await asyncio.wait_for(ws.recv(), 3))
            assert "usage" in again["capabilities"]  # every snapshot keeps advertising it


async def test_bridge_without_usage_does_not_advertise_it():
    async with _bridge(StubHerdr(panes=[]), None) as (url, _server):
        async with websockets.connect(url, additional_headers={"Authorization": "Bearer tok"}) as ws:
            snapshot = json.loads(await asyncio.wait_for(ws.recv(), 3))
            assert "usage" not in snapshot["capabilities"]
    assert "usage" not in bridge_mod._WIRE_CAPABILITIES


# --- runtime connector --------------------------------------------------------


def _connector(reports):
    return Connector(
        ServerConfig(id="cfg-id", url="ws://unused", token="t"),
        on_snapshot=lambda sid, states: None,
        on_event=lambda sid, state: None,
        on_connection=lambda sid, up: None,
        on_usage=lambda sid, offered, providers: reports.append((sid, offered, providers)),
    )


def _snapshot(capabilities):
    return json.dumps(
        {"type": "snapshot", "server_id": "b", "protocol": 3, "capabilities": capabilities, "panes": []}
    )


def test_connector_reports_offer_and_frames_under_its_config_id():
    reports = []
    conn = _connector(reports)
    conn._dispatch(_snapshot(["usage"]))
    conn._dispatch(json.dumps({"type": "usage", "server_id": "b", "providers": usage_to_wire([_codex()])}))
    assert reports == [("cfg-id", True, None), ("cfg-id", True, [_codex()])]


def test_connector_old_bridge_reports_no_offer():
    reports = []
    conn = _connector(reports)
    conn._dispatch(_snapshot(["work_context"]))
    assert reports == [("cfg-id", False, None)]


def test_older_runtime_ignores_usage_frames():
    """An old runtime's decoder falls through to Unknown for new frame types;
    the bridge may therefore push usage to everyone."""
    assert isinstance(decode_inbound(json.dumps({"type": "usage_v2", "server_id": "b"})), Unknown)


async def test_connector_end_to_end_reports_frames_and_disconnect():
    reports = []
    feed = BridgeUsageFeed(FakePoller([_codex()]), "s")
    async with _bridge(StubHerdr(panes=[]), feed) as (url, server):
        conn = Connector(
            ServerConfig(id="agents", url=url, token="tok"),
            on_snapshot=lambda sid, states: None,
            on_event=lambda sid, state: None,
            on_connection=lambda sid, up: None,
            on_usage=lambda sid, offered, providers: reports.append((sid, offered, providers)),
            backoff_base=5.0,
        )
        task = asyncio.create_task(conn.run())
        for _ in range(100):
            if any(p for _sid, _o, p in reports):
                break
            await asyncio.sleep(0.02)
        assert ("agents", True, [_codex()]) in reports
        server.close()
        await server.wait_closed()
        for _ in range(100):
            if reports[-1] == ("agents", False, None):
                break
            await asyncio.sleep(0.02)
        assert reports[-1] == ("agents", False, None)
        conn.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# --- service ------------------------------------------------------------------


def _service(tmp_path, **overrides):
    from herdeck.service import ServiceConfig

    values = {
        "kind": "bridge",
        "home": tmp_path,
        "python": "/opt/herdeck/python",
        "bind": "127.0.0.1",
        "port": 8788,
        "socket_path": tmp_path / "herdr.sock",
        "token_file": tmp_path / "bridge-token",
        "uid": 501,
    }
    values.update(overrides)
    return ServiceConfig(**values)


def _env(config):
    from herdeck.service import render_launch_agent

    return plistlib.loads(render_launch_agent(config))["EnvironmentVariables"]


def test_service_usage_flag_sets_bridge_env(tmp_path):
    from herdeck.service import USAGE_SERVICE_PATH

    assert "HERDECK_BRIDGE_USAGE" not in _env(_service(tmp_path))
    env = _env(_service(tmp_path, usage=True, config_path=tmp_path / "config.toml"))
    assert env["HERDECK_BRIDGE_USAGE"] == "1"
    assert env["HERDECK_USAGE_CONFIG"] == str(tmp_path / "config.toml")
    assert env["PATH"] == USAGE_SERVICE_PATH
    env = _env(_service(tmp_path, usage=True, extra_env=(("PATH", "/custom/bin:/usr/bin"),)))
    assert env["PATH"] == "/custom/bin:/usr/bin" and "HERDECK_USAGE_CONFIG" not in env


def test_service_usage_flag_is_bridge_only(tmp_path):
    from herdeck.service import _config_from_args, _parser

    args = _parser().parse_args(["install", "bridge", "--usage", "--home", str(tmp_path)])
    assert _config_from_args(args).usage is True
    args = _parser().parse_args(["install", "web", "--usage", "--home", str(tmp_path)])
    with pytest.raises(SystemExit, match="--usage"):
        _config_from_args(args)
