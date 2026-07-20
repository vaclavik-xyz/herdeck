import plistlib
import pwd
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


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


def _write_private_token(tmp_path):
    token_file = tmp_path / ".config/herdeck/bridge-token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("existing-secret")
    token_file.chmod(0o600)
    return token_file


def _write_legacy_bridge(tmp_path):
    plist_path = tmp_path / "Library/LaunchAgents/dev.herdeck.bridge.plist"
    plist_path.parent.mkdir(parents=True)
    plist_path.write_text("old agent")
    return plist_path


def test_web_launch_agent_disables_legacy_query_token_by_default(tmp_path):
    from herdeck.service import ServiceConfig, render_launch_agent

    raw = render_launch_agent(
        ServiceConfig(
            kind="web",
            home=tmp_path,
            python="/opt/herdeck/python",
            bind="100.86.178.12",
            port=8801,
        )
    )
    plist = plistlib.loads(raw)

    assert plist["EnvironmentVariables"]["HERDECK_WEB_ALLOW_QUERY_TOKEN"] == "0"
    assert "--allow-query-token" not in plist["ProgramArguments"]


def test_web_launch_agent_preserves_explicit_legacy_opt_in(tmp_path):
    from herdeck.service import ServiceConfig, render_launch_agent

    raw = render_launch_agent(
        ServiceConfig(
            kind="web",
            home=tmp_path,
            python="/opt/herdeck/python",
            bind="127.0.0.1",
            port=8800,
            allow_query_token=True,
        )
    )
    plist = plistlib.loads(raw)

    assert plist["EnvironmentVariables"]["HERDECK_WEB_ALLOW_QUERY_TOKEN"] == "1"
    assert plist["ProgramArguments"][-1] == "--allow-query-token"


def test_bridge_launch_agent_references_token_file_without_secret(tmp_path):
    from herdeck.service import render_launch_agent

    raw = render_launch_agent(_bridge_config(tmp_path))
    plist = plistlib.loads(raw)

    assert plist["Label"] == "dev.herdeck.bridge"
    assert plist["ProgramArguments"] == ["/opt/herdeck/python", "-m", "herdeck.bridge"]
    assert plist["EnvironmentVariables"]["HERDECK_TOKEN_FILE"].endswith("bridge-token")
    assert plist["LimitLoadToSessionType"] == "Background"
    assert "HERDECK_TOKEN" not in plist["EnvironmentVariables"]
    assert b"change-me" not in raw


def test_bridge_system_service_runs_as_target_user_without_login_session(tmp_path):
    from herdeck.service import render_launch_agent

    raw = render_launch_agent(
        _bridge_config(tmp_path, system=True, user_name="admin")
    )
    plist = plistlib.loads(raw)

    assert plist["UserName"] == "admin"
    assert plist["ProcessType"] == "Background"
    assert plist["ThrottleInterval"] == 10
    assert "LimitLoadToSessionType" not in plist


def test_install_bridge_creates_private_token_and_bootstraps_background_agent(tmp_path):
    from herdeck.service import install_service

    calls = []
    token_file = tmp_path / ".config/herdeck/bridge-token"

    plist_path = install_service(
        _bridge_config(tmp_path),
        runner=lambda command: calls.append(command) or 0,
        token_factory=lambda: "generated-secret",
    )

    assert token_file.read_text() == "generated-secret"
    assert token_file.stat().st_mode & 0o777 == 0o600
    assert b"generated-secret" not in plist_path.read_bytes()
    assert calls == [["launchctl", "bootstrap", "user/501", str(plist_path)]]


def test_install_system_bridge_migrates_user_agent_after_verified_bootstrap(tmp_path):
    from herdeck.service import install_service

    calls = []
    installed = []
    _write_private_token(tmp_path)
    old_plist = _write_legacy_bridge(tmp_path)
    config = _bridge_config(tmp_path, system=True, user_name="admin")

    def runner(command):
        calls.append(command)
        if command[:2] == ["sudo", "/usr/bin/install"]:
            installed.append(Path(command[-2]).read_bytes())
        return 0

    plist_path = install_service(config, runner=runner)

    assert plist_path == Path("/Library/LaunchDaemons/dev.herdeck.bridge.plist")
    assert not old_plist.exists()
    assert len(installed) == 1
    assert plistlib.loads(installed[0])["UserName"] == "admin"
    assert calls[:4] == [
        ["launchctl", "print", "user/501/dev.herdeck.bridge"],
        ["launchctl", "bootout", "gui/501/dev.herdeck.bridge"],
        ["launchctl", "bootout", "user/501/dev.herdeck.bridge"],
        ["sudo", "launchctl", "bootout", "system/dev.herdeck.bridge"],
    ]
    assert calls[-3:] == [
        [
            "sudo",
            "/usr/bin/install",
            "-o",
            "root",
            "-g",
            "wheel",
            "-m",
            "0644",
            calls[-3][-2],
            "/Library/LaunchDaemons/dev.herdeck.bridge.plist",
        ],
        [
            "sudo",
            "launchctl",
            "bootstrap",
            "system",
            "/Library/LaunchDaemons/dev.herdeck.bridge.plist",
        ],
        ["launchctl", "print", "system/dev.herdeck.bridge"],
    ]


def test_failed_system_bootstrap_restores_existing_gui_bridge(tmp_path):
    from herdeck.service import install_service

    calls = []
    _write_private_token(tmp_path)
    old_plist = _write_legacy_bridge(tmp_path)
    config = _bridge_config(tmp_path, system=True, user_name="admin")

    def runner(command):
        calls.append(command)
        if command == ["launchctl", "print", "user/501/dev.herdeck.bridge"]:
            return 1
        if command == ["launchctl", "print", "gui/501/dev.herdeck.bridge"]:
            return 0
        if command[:4] == ["sudo", "launchctl", "bootstrap", "system"]:
            return 5
        return 0

    with pytest.raises(SystemExit, match="bootstrap failed"):
        install_service(config, runner=runner)

    assert old_plist.exists()
    assert calls[-2:] == [
        ["sudo", "/bin/rm", "-f", "/Library/LaunchDaemons/dev.herdeck.bridge.plist"],
        ["launchctl", "bootstrap", "gui/501", str(old_plist)],
    ]


def test_failed_legacy_restore_is_reported_as_rollback_failure(tmp_path):
    from herdeck.service import install_service

    _write_private_token(tmp_path)
    old_plist = _write_legacy_bridge(tmp_path)
    config = _bridge_config(tmp_path, system=True, user_name="admin")

    def runner(command):
        if command[:4] == ["sudo", "launchctl", "bootstrap", "system"]:
            return 5
        if command[:3] == ["launchctl", "bootstrap", "user/501"]:
            return 9
        return 0

    with pytest.raises(SystemExit, match="migration and rollback failed"):
        install_service(config, runner=runner)

    assert old_plist.exists()


def test_failed_system_upgrade_restores_previous_daemon(tmp_path):
    from herdeck.service import install_service

    calls = []
    installed = []
    _write_private_token(tmp_path)
    system_dir = tmp_path / "Library/LaunchDaemons"
    system_dir.mkdir(parents=True)
    system_plist = system_dir / "dev.herdeck.bridge.plist"
    system_plist.write_bytes(b"previous daemon")
    config = _bridge_config(
        tmp_path,
        system=True,
        system_dir=system_dir,
        user_name="admin",
    )

    def runner(command):
        calls.append(command)
        if command[:2] == ["sudo", "/usr/bin/install"]:
            installed.append(Path(command[-2]).read_bytes())
        if command == ["launchctl", "print", "system/dev.herdeck.bridge"]:
            return 1
        return 0

    with pytest.raises(SystemExit, match="could not verify"):
        install_service(config, runner=runner)

    assert installed[-1] == b"previous daemon"
    assert calls[-3] == ["sudo", "launchctl", "bootout", "system/dev.herdeck.bridge"]
    assert calls[-1] == ["sudo", "launchctl", "bootstrap", "system", str(system_plist)]


def test_cli_install_system_bridge_builds_daemon_config(monkeypatch, tmp_path):
    from herdeck import service

    captured = []
    monkeypatch.setattr(service, "install_service", lambda config: captured.append(config) or Path("/x"))

    service.main(
        [
            "install",
            "bridge",
            "--home",
            str(tmp_path),
            "--uid",
            str(service.os.getuid()),
            "--system",
            "--bind",
            "100.86.178.12",
        ]
    )

    assert len(captured) == 1
    assert captured[0].system is True
    assert captured[0].user_name == pwd.getpwuid(service.os.getuid()).pw_name
    assert captured[0].home == tmp_path


def test_cli_rejects_system_web_service():
    from herdeck import service

    with pytest.raises(SystemExit, match="supported only for the bridge"):
        service.main(["status", "web", "--system"])


def test_install_migrates_existing_gui_agent_to_background_domain(tmp_path):
    from herdeck.service import ServiceConfig, install_service

    calls = []
    config = ServiceConfig(
        kind="bridge",
        home=tmp_path,
        python="/opt/herdeck/python",
        bind="100.86.178.12",
        port=8788,
        socket_path=tmp_path / ".config/herdr/herdr.sock",
        server_id="workbox",
        token_file=tmp_path / ".config/herdeck/bridge-token",
        uid=501,
    )
    plist_path = tmp_path / "Library/LaunchAgents/dev.herdeck.bridge.plist"
    plist_path.parent.mkdir(parents=True)
    plist_path.write_text("old gui agent")
    config.token_file.parent.mkdir(parents=True)
    config.token_file.write_text("existing-secret")
    config.token_file.chmod(0o600)

    install_service(config, runner=lambda command: calls.append(command) or 0)

    assert calls == [
        ["launchctl", "bootout", "gui/501/dev.herdeck.bridge"],
        ["launchctl", "bootout", "user/501/dev.herdeck.bridge"],
        ["launchctl", "bootstrap", "user/501", str(plist_path)],
    ]


def test_status_and_uninstall_use_launchd_domain_and_remove_plist(tmp_path):
    from herdeck.service import ServiceConfig, service_status, uninstall_service

    calls = []
    config = ServiceConfig(
        kind="web",
        home=tmp_path,
        python="/opt/herdeck/python",
        bind="127.0.0.1",
        port=8800,
        uid=501,
    )
    plist_path = tmp_path / "Library/LaunchAgents/dev.herdeck.web.plist"
    plist_path.parent.mkdir(parents=True)
    plist_path.write_text("placeholder")

    def runner(command):
        calls.append(command)
        return 0

    assert service_status(config, runner=runner) == 0
    uninstall_service(config, runner=runner)

    assert calls == [
        ["launchctl", "print", "user/501/dev.herdeck.web"],
        ["launchctl", "bootout", "user/501/dev.herdeck.web"],
        ["launchctl", "bootout", "gui/501/dev.herdeck.web"],
    ]
    assert not plist_path.exists()


def test_system_status_and_uninstall_use_launchdaemon_domain(tmp_path):
    from herdeck.service import ServiceConfig, service_status, uninstall_service

    calls = []
    config = ServiceConfig(
        kind="bridge",
        home=tmp_path,
        python="/opt/herdeck/python",
        bind="100.86.178.12",
        port=8788,
        system=True,
        user_name="admin",
    )

    def runner(command):
        calls.append(command)
        return 0

    assert service_status(config, runner=runner) == 0
    uninstall_service(config, runner=runner)

    assert calls == [
        ["launchctl", "print", "system/dev.herdeck.bridge"],
        ["sudo", "launchctl", "bootout", "system/dev.herdeck.bridge"],
        ["sudo", "/bin/rm", "-f", "/Library/LaunchDaemons/dev.herdeck.bridge.plist"],
    ]


def test_shipped_service_examples_never_embed_token_values():
    bridge_plist = (ROOT / "deploy/dev.herdeck.bridge.plist").read_text()
    systemd = (ROOT / "deploy/herdeck-bridge.service").read_text()
    web_plist = (ROOT / "deploy/dev.herdeck.web.plist").read_text()

    assert "HERDECK_TOKEN_FILE" in bridge_plist
    assert "HERDECK_TOKEN_FILE" in systemd
    assert "HERDECK_TOKEN</key>" not in bridge_plist
    assert "HERDECK_TOKEN=" not in systemd
    assert "change-me" not in bridge_plist + systemd + web_plist
    assert "<string>Background</string>" in bridge_plist
    assert "<string>Background</string>" in web_plist


def test_install_rejects_unsafe_web_bind(tmp_path):
    from herdeck.service import ServiceConfig, install_service

    config = ServiceConfig(
        kind="web",
        home=tmp_path,
        python="/opt/herdeck/python",
        bind="0.0.0.0",
        port=8800,
        uid=501,
    )

    with pytest.raises(ValueError, match="loopback or a Tailscale"):
        install_service(config, runner=lambda command: 0)


def test_web_launch_agent_includes_reverse_proxy_policy(tmp_path):
    from herdeck.service import ServiceConfig, render_launch_agent

    config = ServiceConfig(
        kind="web",
        home=tmp_path,
        python="/opt/herdeck/python",
        bind="127.0.0.1",
        port=8800,
        base_path="/cockpit/herdeck",
        public_origin="https://cockpit.example",
        frame_ancestors=("https://cockpit.example",),
    )

    environment = plistlib.loads(render_launch_agent(config))["EnvironmentVariables"]

    assert environment["HERDECK_WEB_BASE_PATH"] == "/cockpit/herdeck"
    assert environment["HERDECK_WEB_PUBLIC_ORIGIN"] == "https://cockpit.example"
    assert environment["HERDECK_WEB_FRAME_ANCESTORS"] == "https://cockpit.example"
