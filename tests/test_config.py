import tomllib
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


def test_example_config_uses_profile_schema(monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret123")
    path = Path(__file__).resolve().parents[1] / "config.example.toml"
    cfg = load_config(path)

    assert cfg.meta.active_profile == "work"
    assert {"base", "work", "mobile"} <= set(cfg.meta.profile_names)
    assert set(DEFAULT_START_PROFILES) <= set(cfg.start_profiles)
    assert cfg.view.management in {"launcher_menu", "bottom_row"}
    assert cfg.view.show_profile_on_panel is True
    assert "blocked" in cfg.theme.colors


def test_example_management_row_documents_supported_actions_only():
    path = Path(__file__).resolve().parents[1] / "config.example.toml"
    data = tomllib.loads(path.read_text())

    assert set(data["profiles"]["mobile"]["view"]["bottom_row"]) <= {"profiles", "new_agent"}


def test_readme_describes_answer_profile_overrides_in_unified_model():
    path = Path(__file__).resolve().parents[1] / "README.md"
    section = path.read_text().split("## Adding an agent type", 1)[1].split("\n## ", 1)[0]

    assert "profiles.<name>.answer_profiles" in section


def test_readme_uses_profile_schema_for_notifications():
    path = Path(__file__).resolve().parents[1] / "README.md"
    section = path.read_text().split("## Notifications", 1)[1].split("\n## ", 1)[0]

    assert "[profiles.work.notifications]" in section
    assert "backends" in section


def test_readme_documents_interactive_telegram_security():
    path = Path(__file__).resolve().parents[1] / "README.md"
    section = path.read_text().split("## Notifications", 1)[1].split("\n## ", 1)[0]

    assert "interactive = true" in section
    assert "allowed_user_ids" in section
    assert "message_thread_id" in section
    assert "Reply to this message" in section
    assert "Non-interactive notifications contain only" in section
    assert "Notifications contain only the repo/label" not in section


def test_readme_launcher_example_uses_valid_toml_shape():
    path = Path(__file__).resolve().parents[1] / "README.md"
    section = path.read_text().split("## Adding an agent type", 1)[1].split("\n## ", 1)[0]

    assert "[start_profiles] myagent" not in section
    assert "[start_profiles]" in section


def test_load_resolves_token_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret123")
    cfg = load_config(_write(tmp_path, CONFIG))
    assert cfg.servers[0].id == "workbox"
    assert cfg.servers[0].token == "secret123"
    assert cfg.grid == (5, 3)
    assert cfg.overview_order == ["workbox"]


_OVERLAY_CONFIG = """
active_profile = "work"

[[servers]]
id = "workbox"
url = "ws://x"
token_env = "HERDECK_WORKBOX_TOKEN"

[view]
show_profile_on_panel = true

[notifications]
enabled = true
backends = ["macos"]
on = ["blocked"]
sound = false

[profiles.work]
servers = ["workbox"]
"""


def test_load_config_uses_overlay_profile_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret123")
    path = _write(tmp_path, _OVERLAY_CONFIG)

    cfg = load_config(path)

    assert cfg.meta.active_profile == "work"
    assert cfg.view.show_profile_on_panel is True
    assert cfg.notifications.enabled is True
    assert cfg.servers[0].token == "secret123"


def test_load_config_uses_env_local_profile_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret123")
    path = _write(
        tmp_path,
        _OVERLAY_CONFIG
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


def test_notifications_parses_interactive_telegram_fields(tmp_path):
    cfg = load_config(
        _write(
            tmp_path,
            "[notifications]\n"
            "enabled=true\n"
            'backends=["telegram"]\n'
            "[notifications.telegram]\n"
            'token_env="HERDECK_TG"\n'
            'chat_id="-100123"\n'
            "message_thread_id=456\n"
            "interactive=true\n"
            "allowed_user_ids=[111, 222]\n"
            "prompt_max_chars=777\n",
        )
    )

    tg = cfg.notifications.telegram
    assert tg is not None
    assert tg.token_env == "HERDECK_TG"
    assert tg.chat_id == "-100123"
    assert tg.message_thread_id == 456
    assert tg.interactive is True
    assert tg.allowed_user_ids == [111, 222]
    assert tg.prompt_max_chars == 777


def test_notifications_interactive_defaults_are_safe(tmp_path):
    cfg = load_config(
        _write(
            tmp_path,
            "[notifications]\n"
            "enabled=true\n"
            'backends=["telegram"]\n'
            "[notifications.telegram]\n"
            'token_env="HERDECK_TG"\n'
            'chat_id="42"\n',
        )
    )

    tg = cfg.notifications.telegram
    assert tg is not None
    assert tg.message_thread_id is None
    assert tg.interactive is False
    assert tg.allowed_user_ids == []
    assert tg.prompt_max_chars == 1200


def test_notifications_interactive_rejects_string_bool(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            _write(
                tmp_path,
                "[notifications]\n"
                "enabled=true\n"
                'backends=["telegram"]\n'
                "[notifications.telegram]\n"
                'token_env="HERDECK_TG"\n'
                'chat_id="42"\n'
                'interactive="false"\n',
            )
        )


def test_notifications_allowed_user_ids_rejects_scalar_string(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            _write(
                tmp_path,
                "[notifications]\n"
                "enabled=true\n"
                'backends=["telegram"]\n'
                "[notifications.telegram]\n"
                'token_env="HERDECK_TG"\n'
                'chat_id="42"\n'
                'allowed_user_ids="111"\n',
            )
        )


def test_notifications_allowed_user_ids_rejects_float(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            _write(
                tmp_path,
                "[notifications]\n"
                "enabled=true\n"
                'backends=["telegram"]\n'
                "[notifications.telegram]\n"
                'token_env="HERDECK_TG"\n'
                'chat_id="42"\n'
                "allowed_user_ids=[111.9]\n",
            )
        )


def test_notifications_message_thread_id_rejects_float(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            _write(
                tmp_path,
                "[notifications]\n"
                "enabled=true\n"
                'backends=["telegram"]\n'
                "[notifications.telegram]\n"
                'token_env="HERDECK_TG"\n'
                'chat_id="42"\n'
                "message_thread_id=456.7\n",
            )
        )


@pytest.mark.parametrize("value", ["false", "0.0"])
def test_notifications_message_thread_id_zero_sentinel_rejects_other_types(tmp_path, value):
    with pytest.raises(ConfigError, match="message_thread_id"):
        load_config(
            _write(
                tmp_path,
                "[notifications.telegram]\n"
                'token_env="HERDECK_TG"\n'
                'chat_id="42"\n'
                f"message_thread_id={value}\n",
            )
        )


def test_notifications_prompt_max_chars_rejects_bool(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            _write(
                tmp_path,
                "[notifications]\n"
                "enabled=true\n"
                'backends=["telegram"]\n'
                "[notifications.telegram]\n"
                'token_env="HERDECK_TG"\n'
                'chat_id="42"\n'
                "prompt_max_chars=true\n",
            )
        )


def test_profile_notifications_parse_interactive_telegram_fields(tmp_path):
    cfg = load_config(
        _write(
            tmp_path,
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
            "allowed_user_ids=[111, 222]\n"
            "prompt_max_chars=777\n",
        )
    )

    tg = cfg.notifications.telegram
    assert cfg.meta.active_profile == "work"
    assert cfg.notifications.enabled is True
    assert tg is not None
    assert tg.message_thread_id == 456
    assert tg.interactive is True
    assert tg.allowed_user_ids == [111, 222]
    assert tg.prompt_max_chars == 777


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


def test_example_config_includes_interactive_telegram_fields(monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret123")
    path = Path(__file__).resolve().parents[1] / "config.example.toml"
    text = path.read_text()

    assert "message_thread_id" in text
    assert "interactive = false" in text
    assert "allowed_user_ids" in text


def test_runtime_customization_defaults_on_config():
    from herdeck.config import Config, HardwareConfig, SafetyConfig, ThemeConfig, ViewConfig

    cfg = Config(servers=[], profiles={}, overview_order=[], grid=(5, 3))

    assert isinstance(cfg.theme, ThemeConfig)
    assert cfg.theme.colors["blocked"] == "amber"
    assert cfg.theme.colors["done"] == "cyan"  # done = finished-unseen, its own visible colour
    assert cfg.theme.colors["offline"] == "red"
    assert cfg.theme.server_accents[:2] == ["teal", "violet"]
    assert isinstance(cfg.view, ViewConfig)
    assert cfg.view.management == "launcher_menu"
    assert cfg.view.bottom_row == ["profiles", "notifications", "safety", "theme", "new_agent"]
    assert cfg.view.tile_fields == ["repo", "branch", "status", "time", "server"]
    assert cfg.view.tile_primary is None
    assert cfg.view.tile_secondary is None
    assert isinstance(cfg.safety, SafetyConfig)
    assert cfg.safety.approve_always is True
    assert isinstance(cfg.hardware, HardwareConfig)
    assert cfg.hardware.brightness == 80
    assert cfg.meta.active_profile == "default"
    assert cfg.meta.profile_names == ["default"]
    assert cfg.meta.env_locked_profile is False
