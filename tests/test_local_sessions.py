from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig
from herdeck.deckapp.server import _start_local_session_bridges
from herdeck.deckapp.sessions import (
    LocalSession,
    discover_local_sessions,
    selected_local_sessions,
)


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def test_discovers_default_and_named_sessions(tmp_path):
    default = tmp_path / ".config/herdr/herdr.sock"
    review = tmp_path / ".config/herdr/sessions/review/herdr.sock"
    _touch(default)
    _touch(review)

    sessions = discover_local_sessions(home=tmp_path, getenv={}.get)

    assert [(item.name, item.available, item.selected) for item in sessions] == [
        ("default", True, True),
        ("review", True, False),
    ]
    assert [item.server_id for item in sessions] == ["local", "local:review"]


def test_saved_selection_can_enable_multiple_named_sessions(tmp_path):
    default = tmp_path / ".config/herdr/herdr.sock"
    review = tmp_path / ".config/herdr/sessions/review/herdr.sock"
    _touch(default)
    _touch(review)
    local = tmp_path / "local.toml"
    local.write_text('[local]\nherdr_sessions = ["default", "review"]\n')

    selected = selected_local_sessions(local, home=tmp_path, getenv={}.get)

    assert [item.name for item in selected] == ["default", "review"]


def test_named_env_session_preserves_legacy_single_selection(tmp_path):
    review = tmp_path / ".config/herdr/sessions/review/herdr.sock"
    _touch(review)

    sessions = discover_local_sessions(
        home=tmp_path,
        getenv={"HERDR_SESSION": "review"}.get,
    )

    assert [(item.name, item.selected) for item in sessions if item.available] == [
        ("review", True)
    ]


def test_selected_unavailable_session_remains_visible(tmp_path):
    local = tmp_path / "local.toml"
    local.write_text('[local]\nherdr_sessions = ["review"]\n')

    sessions = discover_local_sessions(local, home=tmp_path, getenv={}.get)

    review = next(item for item in sessions if item.name == "review")
    assert review.selected is True
    assert review.available is False


def test_selected_local_sessions_merge_with_remote_fleet():
    remote = ServerConfig("workbox", "ws://workbox:8788", "remote-token")
    partial = Config(
        servers=[remote],
        profiles=dict(DEFAULT_PROFILES),
        overview_order=["workbox"],
        grid=(5, 3),
    )
    sessions = [
        LocalSession("default", "local", "/tmp/default.sock", True, True),
        LocalSession("review", "local:review", "/tmp/review.sock", True, True),
    ]

    class _Runner:
        next_port = 9000

        def __init__(self, socket_path):
            self.socket_path = socket_path
            self.closed = False

        def start(self):
            type(self).next_port += 1
            return "127.0.0.1", type(self).next_port, f"token-{type(self).next_port}"

        def close(self):
            self.closed = True

    config, runners = _start_local_session_bridges(
        sessions,
        partial=partial,
        runner_factory=_Runner,
    )
    try:
        assert [server.id for server in config.servers] == [
            "local",
            "local:review",
            "workbox",
        ]
        assert config.overview_order == ["local", "local:review", "workbox"]
        assert set(runners) == {"local", "local:review"}
    finally:
        for runner in runners.values():
            runner.close()


def test_failed_session_runner_is_closed():
    session = LocalSession(
        "review",
        "local:review",
        "/tmp/review.sock",
        True,
        True,
    )

    class _FailingRunner:
        instance = None

        def __init__(self, socket_path):
            self.closed = False
            type(self).instance = self

        def start(self):
            raise RuntimeError("bind failed")

        def close(self):
            self.closed = True

    import pytest

    with pytest.raises(RuntimeError, match="bind failed"):
        _start_local_session_bridges([session], runner_factory=_FailingRunner)

    assert _FailingRunner.instance.closed is True


def _short_home():
    # AF_UNIX paths are limited to ~104 bytes; pytest's tmp_path is too long.
    import tempfile

    return tempfile.mkdtemp(prefix="hd", dir="/tmp")


def test_stale_socket_file_after_crash_is_not_available():
    import shutil
    import socket
    from pathlib import Path

    home = Path(_short_home())
    try:
        path = home / ".config/herdr/herdr.sock"
        path.parent.mkdir(parents=True)
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(str(path))
        dead.close()  # the socket file stays behind, nobody listens
        assert path.exists()

        sessions = discover_local_sessions(home=home, getenv={}.get)

        default = next(item for item in sessions if item.name == "default")
        assert default.available is False
        assert selected_local_sessions(home=home, getenv={}.get) == []
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_listening_socket_is_available():
    import shutil
    import socket
    from pathlib import Path

    from herdeck.deckapp.sessions import socket_alive

    home = Path(_short_home())
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        path = home / ".config/herdr/herdr.sock"
        path.parent.mkdir(parents=True)
        server.bind(str(path))
        server.listen(16)

        assert socket_alive(path) is True
        sessions = discover_local_sessions(home=home, getenv={}.get)
        assert next(item for item in sessions if item.name == "default").available is True
    finally:
        server.close()
        shutil.rmtree(home, ignore_errors=True)


def test_socket_alive_missing_path_is_false(tmp_path):
    from herdeck.deckapp.sessions import socket_alive

    assert socket_alive(tmp_path / "absent.sock") is False
