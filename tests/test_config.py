from pathlib import Path

import pytest

from herdeck.config import (
    DEFAULT_PROFILES,
    DEFAULT_START_PROFILES,
    AnswerProfile,
    ConfigError,
    load_config,
)

CONFIG = """
[[servers]]
id = "workbox"
url = "wss://workbox.tailnet.ts.net:8788"
token_env = "HERDECK_WORKBOX_TOKEN"

[deck]
grid = "5x3"
overview_order = ["workbox"]

[answer_profiles.claude]
approve = ["1", "enter"]
approve_always = ["2", "enter"]
deny = ["esc"]
stop = ["ctrl+c"]

[answer_profiles.default]
approve = ["enter"]
deny = ["esc"]
stop = ["ctrl+c"]
"""

SERVERLESS = """
[answer_profiles.codex]
approve = ["y", "enter"]
deny = ["n", "enter"]
stop = ["ctrl+c"]
"""


def _write(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text)
    return p


def test_default_profiles_cover_claude_codex_default():
    assert set(DEFAULT_PROFILES) == {"claude", "codex", "default"}
    assert isinstance(DEFAULT_PROFILES["default"], AnswerProfile)
    assert DEFAULT_PROFILES["claude"].approve == ["1", "enter"]
    assert DEFAULT_PROFILES["claude"].approve_always == ["2", "enter"]
    assert DEFAULT_PROFILES["default"].stop == ["ctrl+c"]


def test_default_start_profiles_include_more_agents():
    expected = {"claude", "codex", "cursor", "gemini", "opencode"}
    assert expected <= set(DEFAULT_START_PROFILES)


def test_example_start_profiles_match_defaults(monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret123")
    path = Path(__file__).resolve().parents[1] / "config.example.toml"
    cfg = load_config(path)
    assert set(DEFAULT_START_PROFILES) <= set(cfg.start_profiles)


def test_load_resolves_token_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret123")
    cfg = load_config(_write(tmp_path, CONFIG))
    assert cfg.servers[0].id == "workbox"
    assert cfg.servers[0].token == "secret123"
    assert cfg.grid == (5, 3)
    assert cfg.overview_order == ["workbox"]


def test_load_config_uses_new_profile_schema(tmp_path, monkeypatch):
    from tests.test_settings import NEW_CONFIG

    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret123")
    path = _write(tmp_path, NEW_CONFIG)

    cfg = load_config(path)

    assert cfg.meta.active_profile == "work"
    assert cfg.view.show_profile_on_panel is True
    assert cfg.notifications.enabled is True
    assert cfg.servers[0].token == "secret123"


def test_load_config_uses_env_local_profile_override(tmp_path, monkeypatch):
    from tests.test_settings import NEW_CONFIG

    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret123")
    path = _write(
        tmp_path,
        NEW_CONFIG
        + """

[profiles.mobile]
extends = "work"
""",
    )
    local = tmp_path / "device-local.toml"
    local.write_text('active_profile = "mobile"\n')
    monkeypatch.setenv("HERDECK_LOCAL_CONFIG", str(local))

    cfg = load_config(path)

    assert cfg.meta.active_profile == "mobile"


def test_missing_token_env_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDECK_WORKBOX_TOKEN", raising=False)
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, CONFIG))


def test_profile_approve_always_defaults_to_approve(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "x")
    cfg = load_config(_write(tmp_path, CONFIG))
    assert cfg.profiles["claude"].approve_always == ["2", "enter"]
    # default profile has no approve_always -> falls back to approve
    assert cfg.profiles["default"].approve_always == ["enter"]


def test_config_without_servers_yields_empty_list(tmp_path):
    cfg = load_config(_write(tmp_path, SERVERLESS))
    assert cfg.servers == []


def test_missing_answer_profiles_fall_back_to_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, '[deck]\ngrid = "5x3"\n'))
    assert cfg.profiles["default"].approve == ["enter"]
    assert cfg.profiles["claude"].approve == ["1", "enter"]


def test_partial_answer_profiles_merge_over_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, SERVERLESS))
    assert cfg.profiles["codex"].approve == ["y", "enter"]
    assert cfg.profiles["default"].approve == ["enter"]


def test_default_profiles_claude_codex_documented():
    assert DEFAULT_PROFILES["claude"].approve == ["1", "enter"]
    assert DEFAULT_PROFILES["codex"].approve == ["y", "enter"]


def test_notifications_default_disabled_when_absent(tmp_path):
    cfg = load_config(_write(tmp_path, '[deck]\ngrid="5x3"\n'))
    assert cfg.notifications.enabled is False
    assert cfg.notifications.on == ["blocked"]


def test_notifications_parsed(tmp_path):
    cfg = load_config(
        _write(tmp_path, '[notifications]\nenabled=true\nsound=false\non=["blocked", "done"]\n')
    )
    assert cfg.notifications.enabled is True and cfg.notifications.sound is False
    assert cfg.notifications.on == ["blocked", "done"]


def test_notifications_backends_default_macos(tmp_path):
    cfg = load_config(_write(tmp_path, '[deck]\ngrid="5x3"\n'))
    assert cfg.notifications.backends == ["macos"]
    assert cfg.notifications.telegram is None


def test_notifications_parses_telegram_and_backends(tmp_path):
    cfg = load_config(
        _write(
            tmp_path,
            '[notifications]\nenabled=true\nbackends=["macos","telegram"]\n'
            '[notifications.telegram]\ntoken_env="HERDECK_TG"\nchat_id=123\n',
        )
    )
    assert cfg.notifications.backends == ["macos", "telegram"]
    assert cfg.notifications.telegram.token_env == "HERDECK_TG"
    assert cfg.notifications.telegram.chat_id == "123"  # coerced to str


def test_notifications_telegram_incomplete_is_skipped(tmp_path):
    # Incomplete telegram table never fails config load (graceful skip);
    # _build_notifier / doctor surface it later.
    cfg = load_config(
        _write(
            tmp_path,
            '[notifications]\nenabled=true\nbackends=["telegram"]\n'
            "[notifications.telegram]\nchat_id=123\n",
        )
    )  # no token_env
    assert cfg.notifications.telegram is None


def test_example_notifications_backends_default(monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret123")
    path = Path(__file__).resolve().parents[1] / "config.example.toml"
    cfg = load_config(path)
    assert "macos" in cfg.notifications.backends


def test_runtime_customization_defaults_on_config():
    from herdeck.config import Config, HardwareConfig, SafetyConfig, ThemeConfig, ViewConfig

    cfg = Config(servers=[], profiles={}, overview_order=[], grid=(5, 3))

    assert isinstance(cfg.theme, ThemeConfig)
    assert cfg.theme.colors["blocked"] == "amber"
    assert cfg.theme.colors["offline"] == "red"
    assert cfg.theme.server_accents[:2] == ["teal", "violet"]
    assert isinstance(cfg.view, ViewConfig)
    assert cfg.view.management == "launcher_menu"
    assert cfg.view.bottom_row == ["profiles", "notifications", "safety", "theme", "new_agent"]
    assert cfg.view.tile_fields == ["repo", "branch", "status", "time", "server"]
    assert isinstance(cfg.safety, SafetyConfig)
    assert cfg.safety.approve_always is True
    assert isinstance(cfg.hardware, HardwareConfig)
    assert cfg.hardware.brightness == 80
    assert cfg.meta.active_profile == "default"
    assert cfg.meta.profile_names == ["default"]
    assert cfg.meta.env_locked_profile is False
