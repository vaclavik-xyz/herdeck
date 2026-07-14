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


def test_read_missing_config_is_empty_for_onboarding(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDECK_PROFILE", raising=False)
    svc = ConfigService(tmp_path / "config.toml", tmp_path / "local.toml")
    payload = svc.read()
    assert payload.pop("revision")  # content hash present even with no files
    assert payload == {
        "base": {},
        "profiles": {},
        "local": {},
        "secrets": {},
        "env_locked": False,
        "active_profile": "default",
        "runtime_deck": None,
    }


def test_read_reports_effective_explicit_runtime_deck(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_store, "_keyring", _FakeKeyring)
    svc = _svc(tmp_path, local='[local]\ndeck = "d200"\n')
    assert svc.read()["runtime_deck"] == "d200"

    monkeypatch.setenv("HERDECK_DECK", "elgato-plugin")
    assert svc.read()["runtime_deck"] == "elgato-plugin"


def test_write_persists_profile_main_chat_override_as_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "real")
    monkeypatch.setattr(secret_store, "_keyring", _FakeKeyring)
    svc = _svc(tmp_path)
    payload = svc.read()
    payload["profiles"]["mobile"].setdefault("notifications", {})["telegram"] = {
        "message_thread_id": 0,
    }

    errors = svc.write(
        {
            "base": payload["base"],
            "profiles": payload["profiles"],
            "local": payload["local"],
            "revision": payload["revision"],
        }
    )

    assert errors == []
    written = _tomllib.loads((tmp_path / "config.toml").read_text())
    assert written["profiles"]["mobile"]["notifications"]["telegram"]["message_thread_id"] == 0

    from herdeck.settings import load_settings, resolve_profile

    resolved = resolve_profile(load_settings(tmp_path / "config.toml", tmp_path / "local.toml"), "mobile")
    assert resolved.config.notifications.telegram.message_thread_id is None


def test_read_reports_env_locked_and_active_profile(tmp_path, monkeypatch):
    monkeypatch.delenv("TOK", raising=False)
    monkeypatch.delenv("TG", raising=False)
    monkeypatch.setattr(secret_store, "_keyring", _FakeKeyring)
    monkeypatch.setenv("HERDECK_PROFILE", "mobile")
    svc = _svc(tmp_path, local='active_profile = "work"\n')
    data = svc.read()
    assert data["env_locked"] is True
    assert data["active_profile"] == "mobile"  # env wins over local


def test_read_active_profile_falls_back_to_local_then_default(tmp_path, monkeypatch):
    monkeypatch.delenv("TOK", raising=False)
    monkeypatch.delenv("TG", raising=False)
    monkeypatch.delenv("HERDECK_PROFILE", raising=False)
    monkeypatch.setattr(secret_store, "_keyring", _FakeKeyring)
    # "mobile" exists in the _svc config profiles, so it is a valid (non-dangling) selection.
    svc = _svc(tmp_path, local='active_profile = "mobile"\n')
    data = svc.read()
    assert data["env_locked"] is False
    assert data["active_profile"] == "mobile"


def test_read_active_profile_defaults_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("TOK", raising=False)
    monkeypatch.delenv("TG", raising=False)
    monkeypatch.delenv("HERDECK_PROFILE", raising=False)
    monkeypatch.setattr(secret_store, "_keyring", _FakeKeyring)
    svc = _svc(tmp_path)  # no local.toml
    data = svc.read()
    assert data["env_locked"] is False
    assert data["active_profile"] == "default"


def test_read_no_config_drops_dangling_active_profile_keeps_hardware(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDECK_PROFILE", raising=False)
    # config.toml absent → no profiles exist, so a stale local active_profile ("work") is a
    # dangling reference that would block the first Apply. It is dropped (effective profile
    # falls back to default), but the rest of local (hardware) survives.
    (tmp_path / "local.toml").write_text('active_profile = "work"\n[hardware]\nbrightness = 55\n')
    svc = ConfigService(tmp_path / "config.toml", tmp_path / "local.toml")
    data = svc.read()
    assert data["base"] == {}
    assert data["profiles"] == {}
    assert data["secrets"] == {}
    assert data["env_locked"] is False
    assert data["active_profile"] == "default"  # dangling "work" normalized away
    assert "active_profile" not in data["local"]  # not round-tripped into write
    assert data["local"]["hardware"]["brightness"] == 55  # hardware preserved


def test_read_write_round_trip_preserves_top_level_active_profile(tmp_path, monkeypatch):
    # A legacy top-level active_profile in config.toml must survive an edit+write, not be
    # dropped (which would silently revert the effective profile to default).
    monkeypatch.setenv("TOK", "real")
    monkeypatch.delenv("HERDECK_PROFILE", raising=False)
    monkeypatch.setattr(secret_store, "_keyring", _FakeKeyring)
    (tmp_path / "config.toml").write_text(
        'active_profile = "work"\n'
        '[[servers]]\nid = "local"\nurl = "ws://x"\ntoken_env = "TOK"\n'
        '[deck]\ngrid = "5x3"\n'
        '[profiles.work]\nservers = ["local"]\n'  # the active profile must resolve
    )
    svc = ConfigService(tmp_path / "config.toml", tmp_path / "local.toml")
    data = svc.read()
    assert data["base"]["active_profile"] == "work"  # carried in base for round-trip
    errors = svc.write({"base": data["base"], "profiles": data["profiles"], "local": data["local"]})
    assert errors == []
    again = svc.read()
    assert again["base"]["active_profile"] == "work"  # survived the write


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


def test_write_blocked_by_structural_error_even_with_missing_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("TOK", raising=False)  # secret missing
    # Use a fresh ConfigService with no pre-existing config.toml on disk.
    svc = ConfigService(tmp_path / "config.toml", tmp_path / "local.toml")
    data = {
        "base": {
            "servers": [{"id": "local", "url": "ws://x", "token_env": "TOK"}],
            "deck": {"grid": "totally-bad-grid"},  # structural error
        },
        "profiles": {},
        "local": {},
    }
    errors = svc.write(data)
    assert errors  # the bad grid must block the write despite the missing secret
    assert not (tmp_path / "config.toml").exists()  # nothing written


def test_set_and_clear_secret_delegate_to_store(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(secret_store, "set_secret", lambda n, v: calls.append(("set", n, v)))
    monkeypatch.setattr(secret_store, "clear_secret", lambda n: calls.append(("clear", n)))
    svc = _svc(tmp_path)
    svc.set_secret("TOK", "v")
    svc.clear_secret("TOK")
    assert calls == [("set", "TOK", "v"), ("clear", "TOK")]


def test_read_roundtrips_hotkeys_section(tmp_path, monkeypatch):
    monkeypatch.delenv("TOK", raising=False)
    monkeypatch.setattr(secret_store, "_keyring", _FakeKeyring)
    text = (
        '[[servers]]\nid="local"\nurl="ws://x"\ntoken_env="TOK"\n'
        '[hotkeys]\ntoggle_deck = "CmdOrCtrl+Shift+D"\n'
    )
    svc = _svc(tmp_path, text=text)
    assert svc.read()["base"]["hotkeys"] == {"toggle_deck": "CmdOrCtrl+Shift+D"}


def test_write_roundtrips_hotkeys_section(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "real")
    monkeypatch.setattr(secret_store, "_keyring", _FakeKeyring)
    svc = _svc(tmp_path)
    data = svc.read()
    data["base"]["hotkeys"] = {"toggle_deck": "Alt+Space"}
    assert svc.write(data) == []  # no structural errors
    assert _tomllib.loads((tmp_path / "config.toml").read_text())["hotkeys"] == {
        "toggle_deck": "Alt+Space"
    }


def test_read_roundtrips_desktop_section(tmp_path, monkeypatch):
    monkeypatch.delenv("TOK", raising=False)
    monkeypatch.setattr(secret_store, "_keyring", _FakeKeyring)
    text = (
        '[[servers]]\nid="local"\nurl="ws://x"\ntoken_env="TOK"\n'
        '[desktop]\nwindow_mode = "floating"\n'
    )
    svc = _svc(tmp_path, text=text)
    assert svc.read()["base"]["desktop"] == {"window_mode": "floating"}


def test_write_roundtrips_desktop_section(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "real")
    monkeypatch.setattr(secret_store, "_keyring", _FakeKeyring)
    svc = _svc(tmp_path)
    data = svc.read()
    data["base"]["desktop"] = {"window_mode": "always_on_top"}
    assert svc.write(data) == []  # no structural errors
    assert _tomllib.loads((tmp_path / "config.toml").read_text())["desktop"] == {
        "window_mode": "always_on_top"
    }


def test_write_rejects_a_stale_revision(tmp_path, monkeypatch):
    """A payload loaded against an older on-disk revision must not clobber
    newer changes (re-onboarding, tray switch, hand edit)
    (audit: editor-staleness-guard)."""
    monkeypatch.setenv("TOK", "x")
    cfg = tmp_path / "config.toml"
    cfg.write_text('[[servers]]\nid="a"\nurl="ws://x"\ntoken_env="TOK"\n')
    svc = ConfigService(cfg, tmp_path / "local.toml")
    payload = svc.read()
    assert payload["revision"]
    # something else changes the file under the editor
    cfg.write_text('[[servers]]\nid="b"\nurl="ws://y"\ntoken_env="TOK"\n')
    body = {
        "base": payload["base"],
        "profiles": payload["profiles"],
        "local": payload["local"],
        "revision": payload["revision"],
    }
    errors = svc.write(body)
    assert errors and errors[0].startswith("stale_revision")
    assert 'id="b"' in cfg.read_text()  # the newer content survived


def test_write_without_revision_stays_compatible(tmp_path, monkeypatch):
    """Rust's read-modify-write (persist_window_mode) sends no revision and
    must keep working."""
    monkeypatch.setenv("TOK", "x")
    cfg = tmp_path / "config.toml"
    cfg.write_text('[[servers]]\nid="a"\nurl="ws://x"\ntoken_env="TOK"\n')
    svc = ConfigService(cfg, tmp_path / "local.toml")
    payload = svc.read()
    body = {"base": payload["base"], "profiles": {}, "local": {}}
    assert svc.write(body) == []


def test_validate_for_write_matches_write_semantics(tmp_path, monkeypatch):
    """Live validation must never flag a missing secret that Apply accepts
    (roborev 4fb7f7a)."""
    monkeypatch.delenv("NEWTOK", raising=False)
    cfg = tmp_path / "config.toml"
    svc = ConfigService(cfg, tmp_path / "local.toml")
    body = {
        "base": {"servers": [{"id": "a", "url": "ws://x", "token_env": "NEWTOK"}]},
        "profiles": {},
        "local": {},
    }
    assert svc.validate_for_write(body) == []  # write() would accept this
    assert svc.validate(body) != []  # ...though full resolution needs the secret
