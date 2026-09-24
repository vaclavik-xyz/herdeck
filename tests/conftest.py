import pytest


@pytest.fixture(autouse=True)
def _isolated_notification_icons(tmp_path, monkeypatch):
    """Banner icon PNGs (notify_icons) land in a per-test dir, never ~/.cache."""
    monkeypatch.setattr(
        "herdeck.notify_icons.default_dir", lambda: str(tmp_path / "notification-icons")
    )


@pytest.fixture(autouse=True)
def _isolated_event_cursor(tmp_path, monkeypatch):
    """The runtime's bridge-event cursor (event_cursor.py) never touches ~/.cache."""
    monkeypatch.setattr(
        "herdeck.deckapp.event_cursor.default_path",
        lambda tag="": str(tmp_path / "runtime" / f"bridge-events-{tag}.json"),
    )


@pytest.fixture(autouse=True)
def _isolated_status_since_state(tmp_path, monkeypatch):
    """The bridge's persisted status-since table never touches ~/.local/state."""
    monkeypatch.setattr(
        "herdeck.status_since.default_state_path",
        lambda name="bridge-status-since.json": str(tmp_path / "state" / name),
    )


@pytest.fixture(autouse=True)
def _isolated_history_store(tmp_path, monkeypatch):
    """The bridge's episode history (history.py) never touches ~/.local/state."""
    monkeypatch.setattr(
        "herdeck.history.default_path",
        lambda name="history.sqlite": str(tmp_path / "state" / name),
    )


@pytest.fixture(autouse=True)
def _isolated_agent_hook_files(tmp_path, monkeypatch):
    """The subagent-hook installer (hooks_install.py) and herdeck-doctor never
    read or write the real ~/.claude/settings.json or ~/.codex/*."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))



@pytest.fixture(autouse=True)
def _isolated_usage_agent(tmp_path, monkeypatch):
    """The usage agent's file (usage_agent.py) never touches ~/.local/state."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
