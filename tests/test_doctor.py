from herdeck.doctor import (
    Check,
    _socket_pane_list,
    check_config,
    check_deck,
    check_optional_deps,
    check_socket,
    collect_checks,
    format_report,
)


def test_check_socket_missing():
    c = check_socket("/nope.sock", exists=lambda p: False, probe=None)
    assert isinstance(c, Check) and c.ok is False and "not found" in c.detail.lower()


def test_check_socket_ok():
    c = check_socket(
        "/s.sock", exists=lambda p: True, probe=lambda path: {"result": {"panes": [1, 2]}}
    )
    assert c.ok is True and "2" in c.detail


def test_check_socket_no_response():
    c = check_socket(
        "/s.sock", exists=lambda p: True, probe=lambda path: (_ for _ in ()).throw(TimeoutError())
    )
    assert c.ok is False and "respond" in c.detail.lower()


def test_check_socket_malformed():
    c = check_socket("/s.sock", exists=lambda p: True, probe=lambda path: {"weird": 1})
    assert c.ok is False


def test_check_socket_malformed_panes_type():
    c = check_socket(
        "/s.sock", exists=lambda p: True, probe=lambda path: {"result": {"panes": "not-a-list"}}
    )
    assert c.ok is False


def test_check_socket_malformed_response_type():
    c = check_socket("/s.sock", exists=lambda p: True, probe=lambda path: [])
    assert c.ok is False


def test_check_socket_malformed_result_type():
    c = check_socket("/s.sock", exists=lambda p: True, probe=lambda path: {"result": []})
    assert c.ok is False


def test_check_config_none_is_local_mode():
    c = check_config(config_path=None, has_servers=False, socket_exists=True, getenv=lambda k: None)
    assert c.ok is True and "local" in c.detail.lower()


def test_check_config_remote_missing_token_redacts():
    c = check_config(
        config_path="/c",
        has_servers=True,
        socket_exists=False,
        token_envs=["HERDECK_TOKEN"],
        getenv=lambda k: None,
    )
    assert c.ok is False
    assert "HERDECK_TOKEN" in c.detail and "missing" in c.detail.lower()


def test_check_config_remote_token_present_not_leaked():
    c = check_config(
        config_path="/c",
        has_servers=True,
        socket_exists=False,
        token_envs=["HERDECK_TOKEN"],
        getenv=lambda k: "supersecret",
    )
    assert c.ok is True and "supersecret" not in c.detail


def test_check_optional_deps_reports_missing():
    c = check_optional_deps(is_available=lambda mod: mod == "PIL")
    assert "PIL" in c.detail
    assert "cairosvg" in c.detail


def test_check_deck_non_invasive():
    c = check_deck(lib_available=lambda mod: False)
    assert c.ok is False and "pip install" in c.detail.lower()


def test_check_deck_elgato_uses_streamdeck_import_name():
    c = check_deck(lib_available=lambda mod: mod == "StreamDeck")
    assert c.ok is True and "Elgato" in c.detail


def test_format_report_marks_pass_and_fail():
    out = format_report([Check("a", True, "ok"), Check("b", False, "bad")])
    assert "a" in out and "b" in out
    assert "✓" in out and "✗" in out


def test_collect_checks_does_not_require_socket_for_remote_config(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(
        """
[[servers]]
id = "remote"
url = "wss://remote.example.test"
token_env = "HERDECK_TOKEN"
"""
    )
    monkeypatch.setenv("HERDECK_CONFIG", str(config))
    monkeypatch.setenv("HERDECK_TOKEN", "secret")
    monkeypatch.setenv("HERDR_SOCKET", str(tmp_path / "missing.sock"))
    import herdeck.doctor as doctor_mod

    monkeypatch.setattr(doctor_mod, "_probe_server", lambda url, token: None)

    checks = {check.name: check for check in collect_checks()}

    assert checks["configuration"].ok is True
    assert checks["herdr socket"].ok is True
    assert "not required" in checks["herdr socket"].detail
    assert checks["server 'remote'"].ok is True  # remote servers are now probed


async def test_socket_pane_list_returns_raw_rpc(monkeypatch):
    from herdeck import bridge

    class FakeSocketHerdr:
        def __init__(self, path):
            self.path = path

        async def _rpc(self, method, params):
            return {"error": {"message": "bad response"}}

        async def list_panes(self):
            return []

    monkeypatch.setattr(bridge, "SocketHerdr", FakeSocketHerdr)

    assert await _socket_pane_list("/s.sock") == {"error": {"message": "bad response"}}


def test_python_m_invocation_runs_main():
    """`python -m herdeck.doctor` must invoke main() (needs a __main__ guard)."""
    import os
    import subprocess
    import sys

    env = {**os.environ, "PYTHONPATH": "src"}
    r = subprocess.run(
        [sys.executable, "-m", "herdeck.doctor"], capture_output=True, text=True, env=env
    )
    assert "herdeck doctor" in r.stdout


def test_check_notifications_disabled():
    from herdeck.config import Notifications
    from herdeck.doctor import check_notifications

    c = check_notifications(Notifications(enabled=False))
    assert c.ok is True and "disabled" in c.detail.lower()


def test_check_notifications_telegram_present_redacts():
    from herdeck.config import Notifications, TelegramConfig
    from herdeck.doctor import check_notifications

    n = Notifications(
        enabled=True, backends=["macos", "telegram"], telegram=TelegramConfig("HERDECK_TG", "42")
    )
    c = check_notifications(n, getenv=lambda k: "SECRET-TOKEN-VALUE")
    assert c.ok is True
    assert "token_env=present" in c.detail and "chat_id=present" in c.detail
    assert "SECRET-TOKEN-VALUE" not in c.detail  # never leak the value


def test_check_notifications_interactive_requires_allowed_users():
    from herdeck.config import Notifications, TelegramConfig
    from herdeck.doctor import check_notifications

    n = Notifications(
        enabled=True,
        backends=["telegram"],
        telegram=TelegramConfig("HERDECK_TG", "42", interactive=True),
    )

    c = check_notifications(n, getenv=lambda k: "SECRET-TOKEN-VALUE")

    assert c.ok is False
    assert "interactive=missing allowed_user_ids" in c.detail
    assert "no usable backend" not in c.detail
    assert "SECRET-TOKEN-VALUE" not in c.detail


def test_check_notifications_interactive_ready_redacts():
    from herdeck.config import Notifications, TelegramConfig
    from herdeck.doctor import check_notifications

    n = Notifications(
        enabled=True,
        backends=["telegram"],
        telegram=TelegramConfig(
            "HERDECK_TG",
            "-100123",
            message_thread_id=456,
            interactive=True,
            allowed_user_ids=[111],
        ),
    )

    c = check_notifications(n, getenv=lambda k: "SECRET-TOKEN-VALUE")

    assert c.ok is True
    assert "interactive=ready" in c.detail
    assert "topic=present" in c.detail
    assert "SECRET-TOKEN-VALUE" not in c.detail
    assert "111" not in c.detail


def test_collect_checks_resolves_active_profile_notifications(tmp_path, monkeypatch):
    from herdeck.doctor import collect_checks

    config = tmp_path / "config.toml"
    config.write_text(
        'active_profile="work"\n'
        "[deck]\n"
        'grid="5x3"\n'
        "[profiles.work]\n"
        "servers=[]\n"
        "[profiles.work.notifications]\n"
        "enabled=true\n"
        'backends=["telegram"]\n'
        "[profiles.work.notifications.telegram]\n"
        'token_env="HERDECK_TG"\n'
        'chat_id="-100123"\n'
        "message_thread_id=456\n"
        "interactive=true\n"
        "allowed_user_ids=[111]\n"
    )
    monkeypatch.setenv("HERDECK_CONFIG", str(config))
    monkeypatch.setenv("HERDECK_TG", "SECRET-TOKEN-VALUE")
    monkeypatch.setenv("HERDR_SOCKET", str(tmp_path / "missing.sock"))

    checks = {check.name: check for check in collect_checks()}

    assert checks["notifications"].ok is True
    assert "interactive=ready" in checks["notifications"].detail
    assert "topic=present" in checks["notifications"].detail
    assert "SECRET-TOKEN-VALUE" not in checks["notifications"].detail
    assert "111" not in checks["notifications"].detail


def test_collect_checks_reports_notifications_when_server_token_missing(tmp_path, monkeypatch):
    from herdeck.doctor import collect_checks

    config = tmp_path / "config.toml"
    config.write_text(
        'active_profile="work"\n'
        "[[servers]]\n"
        'id="remote"\n'
        'url="wss://remote.example.test"\n'
        'token_env="MISSING_SERVER_TOKEN"\n'
        "[deck]\n"
        'grid="5x3"\n'
        "[profiles.work]\n"
        'servers=["remote"]\n'
        "[profiles.work.notifications]\n"
        "enabled=true\n"
        'backends=["telegram"]\n'
        "[profiles.work.notifications.telegram]\n"
        'token_env="HERDECK_TG"\n'
        'chat_id="-100123"\n'
        "interactive=true\n"
    )
    monkeypatch.setenv("HERDECK_CONFIG", str(config))
    monkeypatch.delenv("MISSING_SERVER_TOKEN", raising=False)
    monkeypatch.setenv("HERDECK_TG", "SECRET-TOKEN-VALUE")
    monkeypatch.setenv("HERDR_SOCKET", str(tmp_path / "missing.sock"))

    checks = {check.name: check for check in collect_checks()}

    assert checks["configuration"].ok is False
    assert "MISSING_SERVER_TOKEN=missing" in checks["configuration"].detail
    assert checks["notifications"].ok is False
    assert "interactive=missing allowed_user_ids" in checks["notifications"].detail
    assert "disabled" not in checks["notifications"].detail
    assert "SECRET-TOKEN-VALUE" not in checks["notifications"].detail


def test_collect_checks_reports_invalid_notifications_when_server_token_missing(
    tmp_path, monkeypatch
):
    from herdeck.doctor import collect_checks

    config = tmp_path / "config.toml"
    config.write_text(
        'active_profile="work"\n'
        "[[servers]]\n"
        'id="remote"\n'
        'url="wss://remote.example.test"\n'
        'token_env="MISSING_SERVER_TOKEN"\n'
        "[deck]\n"
        'grid="5x3"\n'
        "[profiles.work]\n"
        'servers=["remote"]\n'
        "[profiles.work.notifications]\n"
        "enabled=true\n"
        'backends=["telegram"]\n'
        "[profiles.work.notifications.telegram]\n"
        'token_env="HERDECK_TG"\n'
        'chat_id="-100123"\n'
        'interactive="false"\n'
    )
    monkeypatch.setenv("HERDECK_CONFIG", str(config))
    monkeypatch.delenv("MISSING_SERVER_TOKEN", raising=False)
    monkeypatch.setenv("HERDECK_TG", "SECRET-TOKEN-VALUE")
    monkeypatch.setenv("HERDR_SOCKET", str(tmp_path / "missing.sock"))

    checks = {check.name: check for check in collect_checks()}

    assert checks["configuration"].ok is False
    assert checks["notifications"].ok is False
    assert "invalid config" in checks["notifications"].detail
    assert "interactive" in checks["notifications"].detail
    assert "disabled" not in checks["notifications"].detail
    assert "SECRET-TOKEN-VALUE" not in checks["notifications"].detail


def test_check_notifications_telegram_missing_token_fails():
    from herdeck.config import Notifications, TelegramConfig
    from herdeck.doctor import check_notifications

    n = Notifications(
        enabled=True, backends=["telegram"], telegram=TelegramConfig("HERDECK_TG", "42")
    )
    c = check_notifications(n, getenv=lambda k: None)
    assert c.ok is False and "token_env=missing" in c.detail


def test_check_notifications_unknown_backend_fails():
    from herdeck.config import Notifications
    from herdeck.doctor import check_notifications

    c = check_notifications(Notifications(enabled=True, backends=["bogus"]))
    assert c.ok is False and "unknown=bogus" in c.detail


def test_check_notifications_empty_backends_fails():
    from herdeck.config import Notifications
    from herdeck.doctor import check_notifications

    c = check_notifications(Notifications(enabled=True, backends=[]))
    assert c.ok is False and "nothing will fire" in c.detail.lower()


def test_check_notifications_macos_only_ok():
    from herdeck.config import Notifications
    from herdeck.doctor import check_notifications

    c = check_notifications(Notifications(enabled=True, backends=["macos"]))
    assert c.ok is True


def test_check_servers_reports_reachable_and_failing():
    """A half-configured remote must not pass with all checkmarks
    (audit: doctor-remote-probe)."""
    from herdeck.config import ServerConfig
    from herdeck.doctor import check_servers

    servers = [ServerConfig("ok", "ws://a:8788", "t"), ServerConfig("bad", "ws://b:8788", "t")]

    def probe(url, token):
        if url == "ws://a:8788":
            return None
        return "token rejected (close 4401) — check token_env/keychain"

    checks = check_servers(servers, probe)
    assert checks[0].name == "server 'ok'" and checks[0].ok
    assert "answered" in checks[0].detail
    assert checks[1].name == "server 'bad'" and not checks[1].ok
    assert "token rejected" in checks[1].detail
    assert "t" != checks[1].detail  # token value itself never printed


def test_collect_checks_probes_remote_servers(tmp_path, monkeypatch):
    import herdeck.doctor as doctor_mod
    from herdeck.doctor import collect_checks

    config = tmp_path / "config.toml"
    config.write_text(
        '[[servers]]\nid = "remote"\nurl = "wss://remote.example.test"\ntoken_env = "HERDECK_TOKEN"\n'
    )
    monkeypatch.setenv("HERDECK_CONFIG", str(config))
    monkeypatch.setenv("HERDECK_TOKEN", "secret")
    monkeypatch.setenv("HERDR_SOCKET", str(tmp_path / "missing.sock"))
    monkeypatch.setattr(doctor_mod, "_probe_server", lambda url, token: "connection refused")

    checks = {c.name: c for c in collect_checks()}
    assert checks["server 'remote'"].ok is False
    assert "connection refused" in checks["server 'remote'"].detail
