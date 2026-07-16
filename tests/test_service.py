import plistlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


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
    from herdeck.service import ServiceConfig, render_launch_agent

    config = ServiceConfig(
        kind="bridge",
        home=tmp_path,
        python="/opt/herdeck/python",
        bind="100.86.178.12",
        port=8788,
        socket_path=tmp_path / ".config/herdr/herdr.sock",
        server_id="workbox",
        token_file=tmp_path / ".config/herdeck/bridge-token",
    )

    raw = render_launch_agent(config)
    plist = plistlib.loads(raw)

    assert plist["Label"] == "dev.herdeck.bridge"
    assert plist["ProgramArguments"] == ["/opt/herdeck/python", "-m", "herdeck.bridge"]
    assert plist["EnvironmentVariables"]["HERDECK_TOKEN_FILE"].endswith("bridge-token")
    assert plist["LimitLoadToSessionType"] == "Background"
    assert "HERDECK_TOKEN" not in plist["EnvironmentVariables"]
    assert b"change-me" not in raw


def test_install_bridge_creates_private_token_and_bootstraps_background_agent(tmp_path):
    from herdeck.service import ServiceConfig, install_service

    calls = []
    token_file = tmp_path / ".config/herdeck/bridge-token"
    config = ServiceConfig(
        kind="bridge",
        home=tmp_path,
        python="/opt/herdeck/python",
        bind="100.86.178.12",
        port=8788,
        socket_path=tmp_path / ".config/herdr/herdr.sock",
        server_id="workbox",
        token_file=token_file,
        uid=501,
    )

    plist_path = install_service(
        config,
        runner=lambda command: calls.append(command) or 0,
        token_factory=lambda: "generated-secret",
    )

    assert token_file.read_text() == "generated-secret"
    assert token_file.stat().st_mode & 0o777 == 0o600
    assert b"generated-secret" not in plist_path.read_bytes()
    assert calls == [["launchctl", "bootstrap", "user/501", str(plist_path)]]


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
