"""herdeck-service kind `usage`: the usage agent LaunchAgent / systemd unit.

Everything runs against a tmp home with a recording runner: nothing is ever
bootstrapped into the real launchd, and ~/Library/LaunchAgents is untouched."""

import json
import plistlib

import pytest
from test_service_maintenance import WHEEL, WHEEL_NAME, FakeTools, _installer


def _usage(tmp_path, **overrides):
    from herdeck.service import ServiceConfig

    values = {
        "kind": "usage",
        "home": tmp_path,
        "python": "/opt/herdeck/venv/bin/python",
        "bind": "127.0.0.1",
        "port": 0,
        "uid": 501,
    }
    values.update(overrides)
    return ServiceConfig(**values)


def _record():
    calls = []
    return calls, (lambda command: calls.append(command) or 0)


def test_launch_agent_runs_in_the_aqua_login_session(tmp_path):
    from herdeck.service import USAGE_SERVICE_PATH, render_launch_agent

    config = _usage(tmp_path, config_path=tmp_path / "config.toml")
    assert config.label == "dev.herdeck.usage"
    assert config.launchd_domain(501) == "gui/501"
    plist = plistlib.loads(render_launch_agent(config))
    assert plist["ProgramArguments"] == ["/opt/herdeck/venv/bin/python", "-m", "herdeck.usage_agent"]
    assert plist["LimitLoadToSessionType"] == "Aqua"
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    assert plist["StandardOutPath"] == str(tmp_path / "Library/Logs/herdeck-usage.log")
    assert plist["EnvironmentVariables"] == {
        "HERDECK_USAGE_CONFIG": str(tmp_path / "config.toml"),
        "PATH": USAGE_SERVICE_PATH,  # codex is a node script: Homebrew's bin
    }
    assert "UserName" not in plist


def test_custom_path_and_no_config(tmp_path):
    from herdeck.service import render_launch_agent

    config = _usage(tmp_path, extra_env=(("PATH", "/nvm/bin:/usr/bin"),))
    env = plistlib.loads(render_launch_agent(config))["EnvironmentVariables"]
    assert env == {"PATH": "/nvm/bin:/usr/bin"}


def test_install_bootstraps_into_the_gui_domain(tmp_path):
    from herdeck.service import install_service

    calls, runner = _record()
    path = install_service(_usage(tmp_path), runner=runner)
    assert path == tmp_path / "Library/LaunchAgents/dev.herdeck.usage.plist"
    assert calls == [["launchctl", "bootstrap", "gui/501", str(path)]]
    assert not (tmp_path / ".config/herdeck/bridge-token").exists()  # no token of its own


def test_system_install_is_rejected(tmp_path):
    from herdeck.service import _config_from_args, _parser, install_service

    with pytest.raises(ValueError, match="only for the bridge"):
        install_service(_usage(tmp_path, system=True), runner=_record()[1])
    args = _parser().parse_args(["install", "usage", "--system", "--home", str(tmp_path)])
    with pytest.raises(SystemExit, match="only for the bridge"):
        _config_from_args(args)


def test_status_and_restart_use_the_gui_domain(tmp_path):
    from herdeck.service import restart_service, service_status_info

    calls, runner = _record()
    info = service_status_info(_usage(tmp_path), runner=runner)
    assert info["label"] == "dev.herdeck.usage" and info["installed"] is False
    assert calls == [["launchctl", "print", "gui/501/dev.herdeck.usage"]]
    restart_service(_usage(tmp_path), runner=runner)
    assert calls[-1] == ["launchctl", "kickstart", "-k", "gui/501/dev.herdeck.usage"]


def test_uninstall_removes_the_unit_and_the_agent_file(tmp_path):
    from herdeck.service import install_service, uninstall_service

    calls, runner = _record()
    install_service(_usage(tmp_path), runner=runner)
    agent_file = tmp_path / ".local/state/herdeck/bridge-usage.json"
    agent_file.parent.mkdir(parents=True)
    agent_file.write_text("{}")
    uninstall_service(_usage(tmp_path), runner=runner)
    assert not agent_file.exists()  # the bridge falls back to its own poller
    assert not (tmp_path / "Library/LaunchAgents/dev.herdeck.usage.plist").exists()
    assert ["launchctl", "bootout", "gui/501/dev.herdeck.usage"] in calls


def test_uninstall_deletes_the_file_only_after_the_agent_stopped(tmp_path):
    """A heartbeat racing the uninstall must not leave a stale file behind."""
    from herdeck.service import install_service, uninstall_service

    install_service(_usage(tmp_path), runner=_record()[1])
    agent_file = tmp_path / ".local/state/herdeck/bridge-usage.json"
    agent_file.parent.mkdir(parents=True)
    def runner(command):
        if command[:2] == ["launchctl", "bootout"]:
            agent_file.write_text("{}")  # the agent's last write before it stops
        return 0

    uninstall_service(_usage(tmp_path), runner=runner)
    assert not agent_file.exists()


def test_uninstall_removes_the_file_under_the_units_xdg_state_home(tmp_path, monkeypatch):
    from herdeck.service import install_service, uninstall_service

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "shell-state"))  # the CLI's own env
    calls, runner = _record()
    state = tmp_path / "unit-state"
    install_service(_usage(tmp_path, extra_env=(("XDG_STATE_HOME", str(state)),)), runner=runner)
    agent_file = state / "herdeck/bridge-usage.json"
    agent_file.parent.mkdir(parents=True)
    agent_file.write_text("{}")
    uninstall_service(_usage(tmp_path), runner=runner)  # no --env on uninstall
    assert not agent_file.exists()


def test_systemd_user_unit(tmp_path):
    from herdeck.service import install_service, render_systemd_unit, uninstall_service

    config = _usage(tmp_path, platform="linux", config_path=tmp_path / "c.toml")
    unit = render_systemd_unit(config)
    assert "Description=Herdeck usage agent" in unit
    assert "ExecStart=/opt/herdeck/venv/bin/python -m herdeck.usage_agent" in unit
    assert f'Environment="HERDECK_USAGE_CONFIG={tmp_path / "c.toml"}"' in unit
    calls, runner = _record()
    path = install_service(config, runner=runner)
    assert path == tmp_path / ".config/systemd/user/herdeck-usage.service"
    assert ["systemctl", "--user", "restart", "herdeck-usage.service"] in calls
    agent_file = tmp_path / ".local/state/herdeck/bridge-usage.json"
    agent_file.parent.mkdir(parents=True)
    agent_file.write_text("{}")
    uninstall_service(config, runner=runner)
    assert not path.exists() and not agent_file.exists()


def test_cli_config_becomes_herdeck_usage_config(tmp_path):
    from herdeck.service import _config_from_args, _parser

    args = _parser().parse_args(
        ["install", "usage", "--home", str(tmp_path), "--config", str(tmp_path / "c.toml")]
    )
    config = _config_from_args(args)
    assert config.kind == "usage" and config.port == 0
    assert config.config_path == tmp_path / "c.toml"
    args = _parser().parse_args(["install", "usage", "--usage", "--home", str(tmp_path)])
    with pytest.raises(SystemExit, match="--usage is supported only for the bridge"):
        _config_from_args(args)


# --- --managed ---------------------------------------------------------------------


def test_managed_usage_reuses_the_bridge_venv_without_reinstalling(tmp_path):
    from herdeck.service import install_managed_usage

    venv = tmp_path / ".local/share/herdeck/bridge-venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").write_text("")
    (venv / "managed.json").write_text(json.dumps({"version": "0.12.0"}))
    tools = FakeTools()
    calls, runner = _record()
    path = install_managed_usage(
        _usage(tmp_path), installer=_installer(tools, {}), runner=runner
    )
    plist = plistlib.loads(path.read_bytes())
    assert plist["ProgramArguments"] == [str(venv / "bin/python"), "-m", "herdeck.usage_agent"]
    assert tools.calls == []  # the running bridge's install is left alone
    assert calls == [["launchctl", "bootstrap", "gui/501", str(path)]]
    install_managed_usage(_usage(tmp_path), "0.12.0", installer=_installer(tools, {}), runner=runner)
    with pytest.raises(SystemExit, match="holds herdeck 0.12.0"):
        install_managed_usage(
            _usage(tmp_path), "0.13.0", installer=_installer(tools, {}), runner=runner
        )
    assert tools.calls == []


def test_managed_usage_installs_the_venv_when_missing(tmp_path):
    from herdeck.service import install_managed_usage

    tools = FakeTools()
    calls, runner = _record()
    path = install_managed_usage(
        _usage(tmp_path), "0.9.1", installer=_installer(tools, {WHEEL_NAME: WHEEL}), runner=runner
    )
    venv = tmp_path / ".local/share/herdeck/bridge-venv"
    assert json.loads((venv / "managed.json").read_text())["version"] == "0.9.1"
    assert plistlib.loads(path.read_bytes())["ProgramArguments"][0] == str(venv / "bin/python")


def test_managed_usage_install_failure_stops_before_the_unit(tmp_path):
    from herdeck.service import install_managed_usage

    calls, runner = _record()
    with pytest.raises(SystemExit, match="managed usage agent install failed"):
        install_managed_usage(
            _usage(tmp_path), "0.9.1", installer=_installer(FakeTools(install_code=1), {}),
            runner=runner,
        )
    assert calls == []


def test_cli_install_usage_managed_and_the_system_bridge_hint(monkeypatch, tmp_path, capsys):
    import herdeck.service as service

    seen = {}

    def fake_usage(config, version):
        seen["usage"] = (config, version)
        return tmp_path / "usage.plist"

    monkeypatch.setattr(service, "install_managed_usage", fake_usage)
    service.main(["install", "usage", "--managed", "--home", str(tmp_path)])
    config, version = seen["usage"]
    assert config.kind == "usage" and version is None
    assert capsys.readouterr().out.strip() == str(tmp_path / "usage.plist")

    monkeypatch.setattr(service, "install_managed_bridge", lambda config, version: tmp_path / "b")
    monkeypatch.setattr(service.pwd, "getpwuid", lambda uid: type("P", (), {"pw_name": "me"}))
    service.main(
        ["install", "bridge", "--system", "--usage", "--managed", "--home", str(tmp_path)]
    )
    assert "herdeck-service install usage --managed" in capsys.readouterr().err
