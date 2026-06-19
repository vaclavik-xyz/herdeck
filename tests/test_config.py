import pytest

from herdeck.config import DEFAULT_PROFILES, AnswerProfile, load_config, ConfigError


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


def test_load_resolves_token_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_WORKBOX_TOKEN", "secret123")
    cfg = load_config(_write(tmp_path, CONFIG))
    assert cfg.servers[0].id == "workbox"
    assert cfg.servers[0].token == "secret123"
    assert cfg.grid == (5, 3)
    assert cfg.overview_order == ["workbox"]


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
    cfg = load_config(_write(tmp_path, "[deck]\ngrid = \"5x3\"\n"))
    assert cfg.profiles["default"].approve == ["enter"]
    assert cfg.profiles["claude"].approve == ["1", "enter"]


def test_partial_answer_profiles_merge_over_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, SERVERLESS))
    assert cfg.profiles["codex"].approve == ["y", "enter"]
    assert cfg.profiles["default"].approve == ["enter"]
