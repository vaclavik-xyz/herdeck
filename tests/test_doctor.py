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
    c = check_socket("/s.sock", exists=lambda p: True,
                     probe=lambda path: {"result": {"panes": [1, 2]}})
    assert c.ok is True and "2" in c.detail


def test_check_socket_no_response():
    c = check_socket("/s.sock", exists=lambda p: True,
                     probe=lambda path: (_ for _ in ()).throw(TimeoutError()))
    assert c.ok is False and "respond" in c.detail.lower()


def test_check_socket_malformed():
    c = check_socket("/s.sock", exists=lambda p: True, probe=lambda path: {"weird": 1})
    assert c.ok is False


def test_check_socket_malformed_panes_type():
    c = check_socket("/s.sock", exists=lambda p: True,
                     probe=lambda path: {"result": {"panes": "not-a-list"}})
    assert c.ok is False


def test_check_socket_malformed_response_type():
    c = check_socket("/s.sock", exists=lambda p: True, probe=lambda path: [])
    assert c.ok is False


def test_check_socket_malformed_result_type():
    c = check_socket("/s.sock", exists=lambda p: True,
                     probe=lambda path: {"result": []})
    assert c.ok is False


def test_check_config_none_is_local_mode():
    c = check_config(config_path=None, has_servers=False, socket_exists=True,
                     getenv=lambda k: None)
    assert c.ok is True and "local" in c.detail.lower()


def test_check_config_remote_missing_token_redacts():
    c = check_config(config_path="/c", has_servers=True, socket_exists=False,
                     token_envs=["HERDECK_TOKEN"], getenv=lambda k: None)
    assert c.ok is False
    assert "HERDECK_TOKEN" in c.detail and "missing" in c.detail.lower()


def test_check_config_remote_token_present_not_leaked():
    c = check_config(config_path="/c", has_servers=True, socket_exists=False,
                     token_envs=["HERDECK_TOKEN"], getenv=lambda k: "supersecret")
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

    checks = {check.name: check for check in collect_checks()}

    assert checks["configuration"].ok is True
    assert checks["herdr socket"].ok is True
    assert "not required" in checks["herdr socket"].detail


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
    r = subprocess.run([sys.executable, "-m", "herdeck.doctor"],
                       capture_output=True, text=True, env=env)
    assert "herdeck doctor" in r.stdout
