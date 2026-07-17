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
