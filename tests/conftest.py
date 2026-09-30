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
    """The usage agent's file (usage_agent.py) and the bridge's usage agent
    installer (usage_agent_install.py) never touch ~/.local/state,
    ~/Library/LaunchAgents or the real launchctl/systemctl."""
    import os
    import sys

    from herdeck import usage_agent_install

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setattr(
        usage_agent_install,
        "default_host",
        lambda: usage_agent_install.Host(
            home=tmp_path / "usage-home",
            uid=501,
            platform=sys.platform,
            env=os.environ,
            run=lambda argv: (1, "launchctl/systemctl are not run in tests"),
        ),
    )
    monkeypatch.setattr(usage_agent_install, "_managed_prefix", lambda: None)


@pytest.fixture(autouse=True)
def _isolated_bridge_settings(tmp_path, monkeypatch):
    """Embedded/standalone bridges never read or write ~/.config/herdeck settings."""
    monkeypatch.setattr(
        "herdeck.bridge._settings_default_path",
        lambda session=None: tmp_path / "bridge-settings" / f"{session or 'bridge'}.toml",
    )


@pytest.fixture(autouse=True)
def _isolated_shared_settings_cache(tmp_path, monkeypatch):
    """The runtime's per-bridge shared settings cache (deckapp/shared_view.py)
    never reads or writes ~/.cache/herdeck/bridge-settings."""
    monkeypatch.setattr(
        "herdeck.deckapp.shared_view.default_cache_dir",
        lambda: tmp_path / "runtime" / "bridge-settings",
    )


@pytest.fixture(autouse=True)
def _isolated_bridge_telegram(tmp_path, monkeypatch):
    """Bridges never read or write ~/.config/herdeck Telegram files, and a
    developer's own bot token never reaches a test bridge."""
    monkeypatch.delenv("HERDECK_TELEGRAM_TOKEN", raising=False)
    monkeypatch.setattr(
        "herdeck.bridge._telegram_default_paths",
        lambda session=None: (
            tmp_path / "bridge-telegram" / f"{session or 'bridge'}.toml",
            tmp_path / "bridge-telegram" / f"{session or 'bridge'}-token",
        ),
        raising=False,
    )
