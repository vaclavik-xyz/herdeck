import herdeck.deckapp.server as server_module
from herdeck.deckapp.server import select_source_kind

REMOTE = ("CONFIG", "SERVER")  # opaque sentinels; the function just passes them through


def k(**kw):
    base = dict(mock_env=False, remote=None, choice=None, socket_path="/s.sock", socket_exists=False)
    base.update(kw)
    return select_source_kind(**base)


def test_mock_env_wins():
    assert k(mock_env=True, remote=REMOTE, choice="local", socket_exists=True) == ("mock", "mock_env")


def test_remote_config():
    assert k(remote=REMOTE) == ("remote", "CONFIG", "SERVER")


def test_local_choice_overrides_remote_config():
    # an explicit local choice wins over a remote config on disk (sticks across restart)
    assert k(remote=REMOTE, choice="local", socket_exists=True) == ("local", "/s.sock")


def test_demo_choice_overrides_remote_config():
    assert k(remote=REMOTE, choice="demo") == ("mock", "demo")


def test_local_when_socket_present():
    assert k(choice="local", socket_exists=True) == ("local", "/s.sock")


def test_local_choice_but_socket_missing():
    assert k(choice="local", socket_exists=False) == ("mock", "local_unavailable")


def test_demo():
    assert k(choice="demo") == ("mock", "demo")


def test_first_run():
    assert k() == ("mock", "first_run")


def test_resolve_source_kind_uses_configured_local_socket(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[[servers]]\nid = "remote"\nurl = "ws://remote"\ntoken_env = "MISSING_TOKEN"\n'
    )
    socket_path = tmp_path / "custom.sock"
    socket_path.touch()
    (tmp_path / "local.toml").write_text(
        f'[local]\nherdr_socket = "{socket_path}"\n'
    )
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    monkeypatch.delenv("HERDR_SOCKET", raising=False)
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.delenv("HERDR_SESSION", raising=False)
    monkeypatch.setattr(server_module, "_default_config_paths", lambda: (str(config_path), None))
    monkeypatch.setattr(server_module, "select_live", lambda: None)
    monkeypatch.setattr("herdeck.deckapp.onboarding.read_choice", lambda path: "local")

    assert server_module._resolve_source_kind() == ("local", str(socket_path))


def test_resolve_source_kind_uses_available_selected_named_session(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "config.toml"
    local_path = tmp_path / "local.toml"
    review_socket = tmp_path / ".config/herdr/sessions/review/herdr.sock"
    review_socket.parent.mkdir(parents=True)
    review_socket.touch()
    local_path.write_text('[local]\nherdr_sessions = ["review"]\n')
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HERDR_SOCKET", raising=False)
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.delenv("HERDR_SESSION", raising=False)
    monkeypatch.setattr(
        server_module,
        "_default_config_paths",
        lambda: (str(config_path), str(local_path)),
    )
    monkeypatch.setattr(server_module, "select_live", lambda: None)
    monkeypatch.setattr("herdeck.deckapp.onboarding.read_choice", lambda path: "local")

    assert server_module._resolve_source_kind() == ("local", str(review_socket))
