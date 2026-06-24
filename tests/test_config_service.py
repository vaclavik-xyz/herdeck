import os
import tomllib as _tomllib

import pytest
import herdeck.secrets as secret_store
from herdeck.config import ConfigError
from herdeck.deckapp.config_service import ConfigService


class _FakeKeyring:
    """In-memory keyring stub — prevents flaky reads from the real OS keychain."""

    def __init__(self):
        self.store = {}

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        del self.store[(service, name)]


CONFIG = """
[[servers]]
id = "local"
url = "ws://x"
token_env = "TOK"

[deck]
grid = "5x3"

[view]
management = "launcher_menu"

[notifications]
enabled = true
[notifications.telegram]
token_env = "TG"
chat_id = "42"

[profiles.mobile]
servers = ["local"]
[profiles.mobile.view]
management = "bottom_row"
"""


def _svc(tmp_path, text=CONFIG, local=None):
    (tmp_path / "config.toml").write_text(text)
    if local is not None:
        (tmp_path / "local.toml").write_text(local)
    return ConfigService(tmp_path / "config.toml", tmp_path / "local.toml")


def test_read_returns_base_profiles_local(tmp_path, monkeypatch):
    monkeypatch.delenv("TOK", raising=False)
    monkeypatch.delenv("TG", raising=False)
    monkeypatch.setattr(secret_store, "_keyring", _FakeKeyring)
    svc = _svc(tmp_path, local='active_profile = "mobile"\n[hardware]\nbrightness = 70\n')
    data = svc.read()
    assert data["base"]["deck"] == {"grid": "5x3"}
    assert data["base"]["view"] == {"management": "launcher_menu"}
    assert data["profiles"]["mobile"]["view"] == {"management": "bottom_row"}
    assert data["local"]["active_profile"] == "mobile"
    assert data["local"]["hardware"]["brightness"] == 70


def test_read_redacts_secrets_to_presence_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "real")
    monkeypatch.delenv("TG", raising=False)
    monkeypatch.setattr(secret_store, "_keyring", _FakeKeyring)
    svc = _svc(tmp_path)
    data = svc.read()
    # No secret VALUE appears anywhere in the payload.
    assert "real" not in repr(data)
    assert data["secrets"]["TOK"] == {"set": True, "source": "env"}
    assert data["secrets"]["TG"] == {"set": False, "source": None}


def test_read_surfaces_profile_only_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("TOK", raising=False)
    monkeypatch.delenv("PTG", raising=False)
    monkeypatch.setattr(secret_store, "_keyring", _FakeKeyring)
    text = (
        '[[servers]]\nid="local"\nurl="ws://x"\ntoken_env="TOK"\n'
        "[profiles.work.notifications.telegram]\n"
        'token_env="PTG"\nchat_id="9"\n'
    )
    svc = _svc(tmp_path, text=text)
    assert "PTG" in svc.read()["secrets"]  # profile-overlay token_env is surfaced


def test_read_missing_config_is_empty_for_onboarding(tmp_path):
    svc = ConfigService(tmp_path / "config.toml", tmp_path / "local.toml")
    assert svc.read() == {"base": {}, "profiles": {}, "local": {}, "secrets": {}}


def test_validate_flags_unknown_server_in_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "real")
    svc = _svc(tmp_path)
    data = {
        "base": {"servers": [{"id": "local", "url": "ws://x", "token_env": "TOK"}]},
        "profiles": {"mobile": {"servers": ["ghost"]}},
        "local": {},
    }
    errors = svc.validate(data)
    assert any("unknown server 'ghost'" in e for e in errors)


def test_validate_clean_config_has_no_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "real")
    svc = _svc(tmp_path)
    data = {
        "base": {"servers": [{"id": "local", "url": "ws://x", "token_env": "TOK"}]},
        "profiles": {},
        "local": {},
    }
    assert svc.validate(data) == []


def test_write_round_trips_and_omits_secret_values(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "real")
    svc = _svc(tmp_path)
    data = {
        "base": {
            "servers": [{"id": "local", "url": "ws://x", "token_env": "TOK"}],
            "deck": {"grid": "4x3"},
        },
        "profiles": {"mobile": {"view": {"management": "bottom_row"}}},
        "local": {"active_profile": "mobile"},
    }
    assert svc.write(data) == []
    text = (tmp_path / "config.toml").read_text()
    assert text.startswith(ConfigService.HEADER)
    assert "real" not in text  # secret value never written
    parsed = _tomllib.loads(text)
    assert parsed["deck"] == {"grid": "4x3"}
    assert parsed["profiles"]["mobile"]["view"] == {"management": "bottom_row"}
    assert _tomllib.loads((tmp_path / "local.toml").read_text())["active_profile"] == "mobile"


def test_write_blocks_on_structural_error_but_not_missing_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("TOK", raising=False)  # secret missing -> NOT a write blocker
    svc = _svc(tmp_path)
    ok_data = {
        "base": {"servers": [{"id": "local", "url": "ws://x", "token_env": "TOK"}]},
        "profiles": {},
        "local": {},
    }
    assert svc.write(ok_data) == []  # missing secret does not block the write
    bad_data = {
        "base": {"servers": [{"id": "local", "url": "ws://x", "token_env": "TOK"}]},
        "profiles": {"a": {"extends": "b"}, "b": {"extends": "a"}},  # cycle = structural
        "local": {},
    }
    errors = svc.write(bad_data)
    assert errors and any("cycle" in e for e in errors)


def test_atomic_write_cleans_temp_on_failure(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    svc = ConfigService(cfg_path, tmp_path / "local.toml")

    def _fail_replace(src, dst):
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", _fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        svc._atomic_write(cfg_path, "hello = 1\n")

    # Temp file must not linger
    assert not (tmp_path / "config.toml.tmp").exists()
    # Destination must not have been created
    assert not cfg_path.exists()


def test_set_active_persists_and_respects_env_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "real")
    monkeypatch.delenv("HERDECK_PROFILE", raising=False)
    svc = _svc(tmp_path)
    assert svc.set_active("mobile") is True
    assert 'active_profile = "mobile"' in (tmp_path / "local.toml").read_text()
    monkeypatch.setenv("HERDECK_PROFILE", "mobile")
    assert svc.set_active("default") is False  # env-locked


def test_create_and_delete_profile_return_new_data(tmp_path):
    svc = _svc(tmp_path)
    data = {"base": {}, "profiles": {"mobile": {}}, "local": {}}
    created = svc.create_profile(data, "work")
    assert created["profiles"]["work"] == {}
    assert "work" not in data["profiles"]  # original untouched
    with pytest.raises(ConfigError, match="default"):
        svc.create_profile(created, "default")
    removed = svc.delete_profile(created, "work")
    assert "work" not in removed["profiles"]
    with pytest.raises(ConfigError, match="unknown profile 'ghost'"):
        svc.delete_profile(created, "ghost")


def test_set_and_clear_secret_delegate_to_store(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(secret_store, "set_secret", lambda n, v: calls.append(("set", n, v)))
    monkeypatch.setattr(secret_store, "clear_secret", lambda n: calls.append(("clear", n)))
    svc = _svc(tmp_path)
    svc.set_secret("TOK", "v")
    svc.clear_secret("TOK")
    assert calls == [("set", "TOK", "v"), ("clear", "TOK")]
