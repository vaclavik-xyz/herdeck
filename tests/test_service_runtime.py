"""herdeck-service: the `runtime` kind, `--from-app`, and systemd --user units."""

import plistlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _runtime_config(tmp_path, **overrides):
    from herdeck.service import ServiceConfig

    values = {
        "kind": "runtime",
        "home": tmp_path,
        "python": "/opt/herdeck/venv/bin/python",
        "bind": "127.0.0.1",
        "port": 0,
        "config_path": tmp_path / ".config/herdeck/config.toml",
        "uid": 501,
    }
    values.update(overrides)
    return ServiceConfig(**values)


def _bridge_config(tmp_path, **overrides):
    from herdeck.service import ServiceConfig

    values = {
        "kind": "bridge",
        "home": tmp_path,
        "python": "/opt/herdeck/python",
        "bind": "100.86.178.12",
        "port": 8788,
        "socket_path": tmp_path / ".config/herdr/herdr.sock",
        "server_id": "workbox",
        "token_file": tmp_path / ".config/herdeck/bridge-token",
        "uid": 501,
    }
    values.update(overrides)
    return ServiceConfig(**values)


def _fake_app(tmp_path, name="herdeck.app"):
    app = tmp_path / "Applications" / name
    binary = app / "Contents/Resources/herdeck-deckapp/herdeck-deckapp"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    return app, binary


# --- runtime kind under launchd ------------------------------------------------


def test_runtime_launch_agent_runs_module_from_interpreter(tmp_path):
    from herdeck.service import render_launch_agent

    raw = render_launch_agent(_runtime_config(tmp_path))
    plist = plistlib.loads(raw)

    assert plist["Label"] == "dev.herdeck.runtime"
    assert plist["ProgramArguments"] == ["/opt/herdeck/venv/bin/python", "-m", "herdeck.runtime"]
    assert plist["EnvironmentVariables"] == {
        "HERDECK_CONFIG": str(tmp_path / ".config/herdeck/config.toml")
    }
    assert plist["KeepAlive"] is True
    assert plist["RunAtLoad"] is True
    # Drives the D200 and posts notifications: it belongs to the login session.
    assert plist["LimitLoadToSessionType"] == "Aqua"
    log = str(tmp_path / "Library/Logs/herdeck-runtime.log")
    assert plist["StandardOutPath"] == log
    assert plist["StandardErrorPath"] == log
    assert b"TOKEN" not in raw


def test_runtime_launch_agent_omits_config_when_not_given(tmp_path):
    from herdeck.service import render_launch_agent

    plist = plistlib.loads(render_launch_agent(_runtime_config(tmp_path, config_path=None)))

    assert plist["EnvironmentVariables"] == {}


def test_runtime_launch_agent_pins_port_only_when_given(tmp_path):
    from herdeck.service import render_launch_agent

    plist = plistlib.loads(render_launch_agent(_runtime_config(tmp_path, port=8790)))

    assert plist["EnvironmentVariables"]["HERDECK_DECKAPP_PORT"] == "8790"


def test_install_runtime_bootstraps_into_gui_domain_without_token(tmp_path):
    from herdeck.service import install_service

    calls = []
    plist_path = install_service(
        _runtime_config(tmp_path),
        runner=lambda command: calls.append(command) or 0,
        token_factory=lambda: pytest.fail("runtime needs no bridge token"),
    )

    assert plist_path == tmp_path / "Library/LaunchAgents/dev.herdeck.runtime.plist"
    assert calls == [["launchctl", "bootstrap", "gui/501", str(plist_path)]]
    assert not (tmp_path / ".config/herdeck/bridge-token").exists()


def test_reinstall_runtime_boots_out_previous_unit_first(tmp_path):
    from herdeck.service import install_service

    calls = []
    config = _runtime_config(tmp_path)
    install_service(config, runner=lambda command: 0)
    plist_path = install_service(config, runner=lambda command: calls.append(command) or 0)

    assert calls == [
        ["launchctl", "bootout", "gui/501/dev.herdeck.runtime"],
        ["launchctl", "bootout", "user/501/dev.herdeck.runtime"],
        ["launchctl", "bootstrap", "gui/501", str(plist_path)],
    ]


def test_runtime_status_and_uninstall_use_gui_domain(tmp_path):
    from herdeck.service import install_service, service_status, uninstall_service

    config = _runtime_config(tmp_path)
    plist_path = install_service(config, runner=lambda command: 0)
    calls = []

    def runner(command):
        calls.append(command)
        return 0

    assert service_status(config, runner=runner) == 0
    uninstall_service(config, runner=runner)

    assert calls[0] == ["launchctl", "print", "gui/501/dev.herdeck.runtime"]
    assert ["launchctl", "bootout", "gui/501/dev.herdeck.runtime"] in calls
    assert not plist_path.exists()


def test_runtime_rejects_system_domain(tmp_path):
    from herdeck.service import install_service

    with pytest.raises(ValueError, match="only for the bridge"):
        install_service(
            _runtime_config(tmp_path, system=True, user_name="admin"), runner=lambda c: 0
        )


# --- runtime from the installed desktop app -----------------------------------


def test_runtime_from_app_runs_bundled_frozen_binary(tmp_path):
    from herdeck.service import render_launch_agent

    app, binary = _fake_app(tmp_path)
    plist = plistlib.loads(render_launch_agent(_runtime_config(tmp_path, from_app=app)))

    assert plist["ProgramArguments"] == [str(binary)]
    assert plist["EnvironmentVariables"]["HERDECK_CONFIG"].endswith("config.toml")
    # A managed sidecar would skip runtime.json, and the app could not attach.
    assert "HERDECK_RUNTIME_MANAGED" not in plist["EnvironmentVariables"]


def test_install_runtime_from_app_bootstraps_bundled_binary(tmp_path):
    from herdeck.service import install_service

    app, binary = _fake_app(tmp_path)
    calls = []
    plist_path = install_service(
        _runtime_config(tmp_path, from_app=app), runner=lambda c: calls.append(c) or 0
    )

    assert plistlib.loads(plist_path.read_bytes())["ProgramArguments"] == [str(binary)]
    assert calls == [["launchctl", "bootstrap", "gui/501", str(plist_path)]]


def test_runtime_from_app_requires_bundled_binary(tmp_path):
    from herdeck.service import install_service

    app = tmp_path / "Applications/herdeck.app"
    app.mkdir(parents=True)

    with pytest.raises(SystemExit, match="no bundled runtime"):
        install_service(_runtime_config(tmp_path, from_app=app), runner=lambda c: 0)
    assert not (tmp_path / "Library/LaunchAgents/dev.herdeck.runtime.plist").exists()


def test_runtime_from_app_is_macos_only(tmp_path):
    from herdeck.service import install_service

    app, _ = _fake_app(tmp_path)

    with pytest.raises(SystemExit, match="macOS"):
        install_service(
            _runtime_config(tmp_path, from_app=app, platform="linux"), runner=lambda c: 0
        )


def test_from_app_is_only_for_runtime(tmp_path):
    from herdeck.service import render_launch_agent

    app, _ = _fake_app(tmp_path)

    with pytest.raises(ValueError, match="runtime"):
        render_launch_agent(_bridge_config(tmp_path, from_app=app))


def test_cli_from_app_defaults_to_applications_bundle(tmp_path, monkeypatch):
    from herdeck import service

    captured = []
    monkeypatch.setattr(service, "install_service", lambda config: captured.append(config) or tmp_path)
    monkeypatch.setattr(service.sys, "platform", "darwin")

    service.main(["install", "runtime", "--home", str(tmp_path), "--uid", "501", "--from-app"])

    assert captured[0].from_app == Path("/Applications/herdeck.app")
    assert captured[0].kind == "runtime"
    assert captured[0].platform == "darwin"


def test_cli_from_app_resolves_explicit_bundle(tmp_path, monkeypatch):
    from herdeck import service

    app, _ = _fake_app(tmp_path)
    captured = []
    monkeypatch.setattr(service, "install_service", lambda config: captured.append(config) or tmp_path)

    service.main(
        ["install", "runtime", "--home", str(tmp_path), "--uid", "501", "--from-app", str(app)]
    )

    assert captured[0].from_app == app.resolve()


def test_cli_rejects_from_app_for_other_kinds(tmp_path):
    from herdeck import service

    with pytest.raises(SystemExit):
        service.main(["install", "bridge", "--home", str(tmp_path), "--from-app"])


# --- systemd --user ---------------------------------------------------------------


def test_runtime_systemd_unit_runs_module_and_restarts(tmp_path):
    from herdeck.service import render_systemd_unit

    unit = render_systemd_unit(_runtime_config(tmp_path, platform="linux"))

    assert "ExecStart=/opt/herdeck/venv/bin/python -m herdeck.runtime\n" in unit
    assert f'Environment="HERDECK_CONFIG={tmp_path}/.config/herdeck/config.toml"\n' in unit
    assert "Restart=always\n" in unit
    assert "WantedBy=default.target\n" in unit
    assert "TOKEN" not in unit


def test_bridge_systemd_unit_references_token_file_without_secret(tmp_path):
    from herdeck.service import render_systemd_unit

    unit = render_systemd_unit(_bridge_config(tmp_path, platform="linux"))

    assert "ExecStart=/opt/herdeck/python -m herdeck.bridge\n" in unit
    assert f'Environment="HERDECK_TOKEN_FILE={tmp_path}/.config/herdeck/bridge-token"\n' in unit
    assert 'Environment="HERDECK_BIND=100.86.178.12"\n' in unit
    assert "HERDECK_TOKEN=" not in unit


def test_systemd_unit_escapes_specifiers_and_quotes(tmp_path):
    from herdeck.service import render_systemd_unit

    unit = render_systemd_unit(
        _runtime_config(
            tmp_path,
            platform="linux",
            python="/opt/my venv/bin/python",
            config_path=Path('/cfg/100%"x".toml'),
        )
    )

    assert 'ExecStart="/opt/my venv/bin/python" -m herdeck.runtime\n' in unit
    assert 'Environment="HERDECK_CONFIG=/cfg/100%%\\"x\\".toml"\n' in unit


def test_install_runtime_systemd_writes_user_unit_and_starts_it(tmp_path):
    from herdeck.service import install_service

    calls = []
    unit_path = install_service(
        _runtime_config(tmp_path, platform="linux"),
        runner=lambda command: calls.append(command) or 0,
    )

    assert unit_path == tmp_path / ".config/systemd/user/herdeck-runtime.service"
    assert "herdeck.runtime" in unit_path.read_text()
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "herdeck-runtime.service"],
        ["systemctl", "--user", "restart", "herdeck-runtime.service"],
    ]


def test_install_bridge_systemd_creates_private_token(tmp_path):
    from herdeck.service import install_service

    unit_path = install_service(
        _bridge_config(tmp_path, platform="linux"),
        runner=lambda command: 0,
        token_factory=lambda: "generated-secret",
    )

    token_file = tmp_path / ".config/herdeck/bridge-token"
    assert token_file.read_text() == "generated-secret"
    assert token_file.stat().st_mode & 0o777 == 0o600
    assert "generated-secret" not in unit_path.read_text()


def test_install_systemd_fails_loudly_when_start_fails(tmp_path):
    from herdeck.service import install_service

    def runner(command):
        return 1 if "restart" in command else 0

    with pytest.raises(SystemExit, match="systemctl"):
        install_service(_runtime_config(tmp_path, platform="linux"), runner=runner)


def test_systemd_rejects_system_flag(tmp_path):
    from herdeck.service import install_service

    with pytest.raises(SystemExit, match="launchd"):
        install_service(
            _bridge_config(tmp_path, platform="linux", system=True, user_name="admin"),
            runner=lambda c: 0,
        )


def test_systemd_status_and_uninstall(tmp_path):
    from herdeck.service import install_service, service_status, uninstall_service

    config = _runtime_config(tmp_path, platform="linux")
    unit_path = install_service(config, runner=lambda command: 0)
    calls = []

    def runner(command):
        calls.append(command)
        return 0

    assert service_status(config, runner=runner) == 0
    uninstall_service(config, runner=runner)

    assert calls == [
        ["systemctl", "--user", "status", "--no-pager", "herdeck-runtime.service"],
        ["systemctl", "--user", "disable", "--now", "herdeck-runtime.service"],
        ["systemctl", "--user", "daemon-reload"],
    ]
    assert not unit_path.exists()


def test_stale_legacy_app_plist_is_gone():
    # deploy/com.herdeck.app.plist launched the legacy herdeck.app with an inline
    # token and no log path; `herdeck-service install runtime` replaces it.
    assert not (ROOT / "deploy/com.herdeck.app.plist").exists()
