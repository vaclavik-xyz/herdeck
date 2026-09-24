"""The bridge's `usage_agent` message (usage_agent_install.py): install /
remove / report the usage agent for the bridge's own user, through a fake
host (tmp home, recording launchctl) — never the real launchd."""

import asyncio
import contextlib
import json
import plistlib
from pathlib import Path

import pytest
import websockets
from test_bridge_security import raw_pane

import herdeck.usage_agent_install as uai
from herdeck import usage_agent
from herdeck.bridge import StubHerdr, _serve_connection
from herdeck.service import USAGE_SERVICE_PATH


class Launchd:
    """Plays launchctl/systemctl: records argv, answers by rules."""

    def __init__(self, gui=True, running=True, fail=()):
        self.calls: list[list[str]] = []
        self.gui = gui
        self.running = running
        self.fail = set(fail)

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[:2] == ["launchctl", "print"]:
            target = argv[2]
            if target.count("/") == 1:  # the domain itself: gui/<uid>
                return (0, "") if self.gui else (113, "Could not find domain")
            return (0, "state = running\n") if self.running else (0, "state = waiting\n")
        if argv[:2] == ["systemctl", "--user"] and argv[2] == "is-active":
            return (0, "") if self.running else (3, "")
        return (1, "failed") if argv[1] in self.fail else (0, "")


def _host(tmp_path, launchd=None, platform="darwin", **env):
    return uai.Host(
        home=tmp_path / "home",
        uid=501,
        platform=platform,
        env={"XDG_STATE_HOME": str(tmp_path / "state"), **env},
        run=launchd or Launchd(),
    )


def _plist(tmp_path):
    path = tmp_path / "home/Library/LaunchAgents/dev.herdeck.usage.plist"
    return plistlib.loads(path.read_bytes())


def _venv(tmp_path):
    venv = tmp_path / "home/.local/share/herdeck/bridge-venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").write_text("")
    return venv


def test_status_without_anything_installed(tmp_path):
    data = uai.apply("status", _host(tmp_path), managed_prefix=lambda: None)
    assert data == {
        "action": "status",
        "ok": True,
        "code": "ok",
        "error": None,
        "installed": False,
        "running": False,
        "managed": False,
        "gui_session": True,
        "bridge_usage": False,
        "file": str(tmp_path / "state/herdeck/bridge-usage.json"),
        "file_age_s": None,
        "fresh": False,
        "providers": [],
    }


def test_install_from_the_managed_venv_passes_config_path_and_state(tmp_path):
    launchd = Launchd()
    venv = _venv(tmp_path)
    host = _host(
        tmp_path,
        launchd,
        HERDECK_BRIDGE_USAGE="1",
        HERDECK_USAGE_CONFIG=str(tmp_path / "config.toml"),
        PATH="/nvm/bin:/usr/bin",
    )
    data = uai.apply("install", host, managed_prefix=lambda: venv)
    assert data["ok"] is True and data["code"] == "ok"
    assert data["installed"] is True and data["running"] is True and data["managed"] is True
    assert data["bridge_usage"] is True
    plist = _plist(tmp_path)
    assert plist["ProgramArguments"] == [str(venv / "bin/python"), "-m", "herdeck.usage_agent"]
    assert plist["LimitLoadToSessionType"] == "Aqua"
    assert plist["EnvironmentVariables"] == {
        "HERDECK_USAGE_CONFIG": str(tmp_path / "config.toml"),
        "PATH": "/nvm/bin:/usr/bin",  # the bridge unit's custom PATH
        "XDG_STATE_HOME": str(tmp_path / "state"),  # writes where this bridge reads
    }
    unit = str(tmp_path / "home/Library/LaunchAgents/dev.herdeck.usage.plist")
    assert ["launchctl", "bootstrap", "gui/501", unit] in launchd.calls


def test_install_default_path_is_not_passed_through(tmp_path):
    host = _host(tmp_path, HERDECK_BRIDGE_USAGE="1", PATH=USAGE_SERVICE_PATH)
    uai.apply("install", host, managed_prefix=lambda: None)
    env = _plist(tmp_path)["EnvironmentVariables"]
    assert env["PATH"] == USAGE_SERVICE_PATH and "HERDECK_USAGE_CONFIG" not in env
    # a bridge without usage has launchd's minimal PATH: never copied
    uai.apply("install", _host(tmp_path, PATH="/usr/bin:/bin"), managed_prefix=lambda: None)
    assert _plist(tmp_path)["EnvironmentVariables"]["PATH"] == USAGE_SERVICE_PATH


def test_install_without_a_gui_session_is_a_clear_error(tmp_path):
    launchd = Launchd(gui=False)
    data = uai.apply("install", _host(tmp_path, launchd), managed_prefix=lambda: None)
    assert data["ok"] is False and data["code"] == "no_gui_session"
    assert "log in" in data["error"] and data["gui_session"] is False
    assert data["installed"] is False
    assert not any(c[:2] == ["launchctl", "bootstrap"] for c in launchd.calls)


def test_install_failure_is_reported(tmp_path):
    launchd = Launchd(fail={"bootstrap"})
    data = uai.apply("install", _host(tmp_path, launchd), managed_prefix=lambda: None)
    assert data["ok"] is False and data["code"] == "failed"
    assert "bootstrap failed" in data["error"]


def test_frozen_bridge_without_a_managed_venv_is_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr(uai.sys, "frozen", True, raising=False)
    data = uai.apply("install", _host(tmp_path), managed_prefix=lambda: None)
    assert data["code"] == "unsupported" and "--managed" in data["error"]


def test_linux_installs_a_systemd_user_unit_without_a_gui_check(tmp_path):
    launchd = Launchd()
    data = uai.apply("install", _host(tmp_path, launchd, platform="linux"), managed_prefix=lambda: None)
    assert data["ok"] is True and data["gui_session"] is None and data["running"] is True
    assert (tmp_path / "home/.config/systemd/user/herdeck-usage.service").exists()
    assert not any(c[0] == "launchctl" for c in launchd.calls)


def test_status_reports_the_file(tmp_path):
    host = _host(tmp_path)
    usage_agent.write_file(
        usage_agent.default_path(host.env, host.home),
        [{"provider": "codex", "windows": []}, {"provider": "claude", "windows": []}, "junk"],
        60,
        1000.0,
    )
    data = uai.status(host, now=1010.0)
    assert data["file_age_s"] == 10.0 and data["fresh"] is True
    assert data["providers"] == ["codex", "claude"]
    assert uai.status(host, now=2000.0)["fresh"] is False


def test_uninstall_removes_unit_and_file(tmp_path):
    launchd = Launchd()
    host = _host(tmp_path, launchd)
    uai.apply("install", host, managed_prefix=lambda: None)
    path = usage_agent.default_path(host.env, host.home)
    usage_agent.write_file(path, [], 60, 1.0)
    data = uai.apply("uninstall", host, managed_prefix=lambda: None)
    assert data["ok"] is True and data["installed"] is False and data["running"] is False
    assert not path.exists()
    assert ["launchctl", "bootout", "gui/501/dev.herdeck.usage"] in launchd.calls


def test_unknown_action_raises():
    with pytest.raises(ValueError):
        uai.apply("wipe", managed_prefix=lambda: None)


async def test_bridge_reply_shapes(tmp_path, monkeypatch):
    host = _host(tmp_path)
    ok = await uai.bridge_reply({"req": "u1", "action": "status"}, host=host)
    assert ok["type"] == "result" and ok["req"] == "u1" and ok["data"]["action"] == "status"
    bad = await uai.bridge_reply({"req": "u2", "action": "wipe"})
    assert bad == {"type": "error", "req": "u2", "message": "usage_agent: invalid action"}

    def slow(*a, **kw):
        import time

        time.sleep(0.2)

    monkeypatch.setattr(uai, "apply", slow)
    late = await uai.bridge_reply({"req": 3, "action": "status"}, timeout=0.01)
    assert late == {"type": "error", "req": "", "message": "usage_agent: timed out"}


# --- over the bridge's WebSocket ------------------------------------------------------


@contextlib.asynccontextmanager
async def _bridge():
    clients: dict = {}

    async def handler(ws):
        await _serve_connection(
            ws, StubHerdr(panes=[raw_pane()]), "s", "full", clients, "/unused.sock",
            readonly_token="view",
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
    return json.loads(await asyncio.wait_for(ws.recv(), 5))


async def test_full_token_installs_and_readonly_is_refused(tmp_path, monkeypatch):
    launchd = Launchd()
    monkeypatch.setattr(uai, "default_host", lambda: _host(tmp_path, launchd))
    async with _bridge() as url:
        async with websockets.connect(url, additional_headers={"Authorization": "Bearer view"}) as ws:
            greeting = json.loads(await ws.recv())
            assert "usage_agent" in greeting["capabilities"]
            reply = await _roundtrip(ws, {"type": "usage_agent", "req": "r", "action": "install"})
            assert reply["type"] == "error" and "read-only token" in reply["message"]
        assert launchd.calls == []
        async with websockets.connect(url, additional_headers={"Authorization": "Bearer full"}) as ws:
            await ws.recv()
            reply = await _roundtrip(ws, {"type": "usage_agent", "req": "i", "action": "install"})
            assert reply["type"] == "result" and reply["req"] == "i"
            assert reply["data"]["ok"] is True and reply["data"]["installed"] is True
            reply = await _roundtrip(ws, {"type": "usage_agent", "req": "s"})  # default: status
            assert reply["data"]["action"] == "status" and reply["data"]["installed"] is True
            reply = await _roundtrip(ws, {"type": "usage_agent", "req": "x", "action": "rm"})
            assert reply["type"] == "error" and reply["req"] == "x"
    assert (tmp_path / "home/Library/LaunchAgents/dev.herdeck.usage.plist").exists()


# --- restart after a bridge self-update -----------------------------------------------


def test_restart_after_update_kickstarts_an_agent_from_that_venv(tmp_path):
    launchd = Launchd()
    host = _host(tmp_path, launchd)
    venv = _venv(tmp_path)
    assert uai.restart_after_update(venv, host) is False  # nothing installed
    uai.apply("install", host, managed_prefix=lambda: venv)
    launchd.calls.clear()
    assert uai.restart_after_update(venv, host) is True
    assert launchd.calls == [["launchctl", "kickstart", "-k", "gui/501/dev.herdeck.usage"]]
    launchd.calls.clear()
    assert uai.restart_after_update(tmp_path / "other-venv", host) is False
    assert launchd.calls == []


def test_restart_after_update_skips_an_agent_outside_the_venv(tmp_path):
    launchd = Launchd()
    host = _host(tmp_path, launchd)
    uai.apply("install", host, managed_prefix=lambda: None)  # runs sys.executable
    launchd.calls.clear()
    assert uai.restart_after_update(_venv(tmp_path), host) is False
    assert launchd.calls == []


def test_restart_after_update_linux_and_failure(tmp_path):
    launchd = Launchd(fail={"--user"})
    host = _host(tmp_path, launchd, platform="linux")
    venv = _venv(tmp_path)
    uai.apply("install", _host(tmp_path, Launchd(), platform="linux"), managed_prefix=lambda: venv)
    assert uai.restart_after_update(venv, host) is False  # systemctl failed: logged, not raised
    assert launchd.calls == [["systemctl", "--user", "restart", "herdeck-usage.service"]]


async def test_self_update_restarts_the_usage_agent(tmp_path):
    from test_bridge_self_update import TARGET, Fakes, Sink

    fakes, sink = Fakes(tmp_path), Sink()
    restarted = []
    updater = fakes.updater(restart_helpers=lambda env: restarted.append(env.prefix))
    await updater.handle({"type": "update", "req": "u1", "version": TARGET}, sink)
    assert sink.result["data"]["restarting"] is True
    assert restarted == [fakes.env.prefix] and fakes.exits == 1


async def test_self_update_survives_a_failing_helper_restart(tmp_path):
    from test_bridge_self_update import TARGET, Fakes, Sink

    fakes, sink = Fakes(tmp_path), Sink()

    def boom(env):
        raise RuntimeError("launchctl exploded")

    await fakes.updater(restart_helpers=boom).handle(
        {"type": "update", "req": "u1", "version": TARGET}, sink
    )
    assert sink.result["data"]["restarting"] is True and fakes.exits == 1


async def test_self_update_default_restarts_through_usage_agent_install(tmp_path, monkeypatch):
    from test_bridge_self_update import TARGET, Fakes, Sink

    seen = []
    monkeypatch.setattr(uai, "restart_after_update", lambda prefix: seen.append(Path(prefix)))
    fakes, sink = Fakes(tmp_path), Sink()
    await fakes.updater().handle({"type": "update", "req": "u1", "version": TARGET}, sink)
    assert seen == [Path(fakes.env.prefix)]


async def test_a_failed_update_restarts_nothing(tmp_path):
    from test_bridge_self_update import Fakes, Sink

    fakes, sink = Fakes(tmp_path), Sink()
    restarted = []
    await fakes.updater(restart_helpers=restarted.append).handle(
        {"type": "update", "req": "u1", "version": "not-a-version"}, sink
    )
    assert restarted == [] and fakes.exits == 0
