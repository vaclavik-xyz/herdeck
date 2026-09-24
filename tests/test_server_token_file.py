"""[[servers]] token resolution: env (token_env) -> token_file -> keychain.

A launchd/systemd runtime has no shell env and its unit refuses TOKEN env names,
so ``token_file`` is how a service reads a token the keychain does not hold.
"""

import os

import pytest

from herdeck import secrets
from herdeck.config import ConfigError
from herdeck.settings import (
    TokenNotFoundError,
    assume_tokens_present,
    load_settings,
    resolve_profile,
    resolve_server_token,
)


class FakeKeyring:
    def __init__(self, store=None):
        self.store = dict(store or {})

    def get_password(self, service, name):
        return self.store.get(name)


@pytest.fixture
def keychain(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(secrets, "_keyring", lambda: fake)
    monkeypatch.delenv("TOK", raising=False)
    return fake.store


def _token_file(tmp_path, text="file-token\n", mode=0o600):
    path = tmp_path / "token"
    path.write_text(text)
    os.chmod(path, mode)
    return path


def test_env_wins_over_file_and_keychain(tmp_path, keychain, monkeypatch):
    keychain["TOK"] = "kc-token"
    monkeypatch.setenv("TOK", "env-token")
    raw = {"id": "local", "token_env": "TOK", "token_file": str(_token_file(tmp_path))}
    assert resolve_server_token(raw) == ("env-token", "env")


def test_file_wins_over_keychain_and_is_stripped(tmp_path, keychain):
    keychain["TOK"] = "kc-token"
    raw = {"id": "local", "token_env": "TOK", "token_file": str(_token_file(tmp_path))}
    assert resolve_server_token(raw) == ("file-token", "file")


def test_keychain_when_token_file_missing(tmp_path, keychain):
    keychain["TOK"] = "kc-token"
    raw = {"id": "local", "token_env": "TOK", "token_file": str(tmp_path / "absent")}
    assert resolve_server_token(raw) == ("kc-token", "keychain")


def test_keychain_only_is_unchanged(keychain):
    keychain["TOK"] = "kc-token"
    assert resolve_server_token({"id": "local", "token_env": "TOK"}) == ("kc-token", "keychain")


def test_token_file_alone_without_token_env(tmp_path, keychain):
    raw = {"id": "local", "token_file": str(_token_file(tmp_path))}
    assert resolve_server_token(raw) == ("file-token", "file")


def test_token_file_tilde_is_expanded(tmp_path, keychain, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _token_file(tmp_path)
    assert resolve_server_token({"id": "local", "token_file": "~/token"}) == ("file-token", "file")


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660])
def test_group_or_world_readable_file_is_refused(tmp_path, keychain, mode):
    keychain["TOK"] = "kc-token"  # a readable-by-others file is refused, not skipped
    raw = {"id": "local", "token_env": "TOK", "token_file": str(_token_file(tmp_path, mode=mode))}
    with pytest.raises(ConfigError, match="chmod 600") as exc:
        resolve_server_token(raw)
    assert "file-token" not in str(exc.value)
    assert f"{mode:04o}" in str(exc.value)


def test_owner_only_stricter_mode_is_accepted(tmp_path, keychain):
    raw = {"id": "local", "token_file": str(_token_file(tmp_path, mode=0o400))}
    assert resolve_server_token(raw) == ("file-token", "file")


def test_directory_is_not_a_token_file(tmp_path, keychain):
    folder = tmp_path / "dir"
    folder.mkdir(mode=0o700)
    with pytest.raises(ConfigError, match="not a regular file"):
        resolve_server_token({"id": "local", "token_file": str(folder)})


def test_empty_token_file_is_refused(tmp_path, keychain):
    raw = {"id": "local", "token_file": str(_token_file(tmp_path, text=" \n"))}
    with pytest.raises(ConfigError, match="is empty"):
        resolve_server_token(raw)


def test_nothing_resolves_names_every_source_and_the_server(tmp_path, keychain):
    raw = {"id": "local", "token_env": "TOK", "token_file": str(tmp_path / "absent")}
    with pytest.raises(TokenNotFoundError) as exc:
        resolve_server_token(raw)
    message = str(exc.value)
    assert exc.value.server_id == "local"
    assert "bridge token for server 'local' not found" in message
    assert "env var 'TOK' is not set" in message
    assert "absent" in message and "no keychain entry 'TOK'" in message


def test_server_needs_a_token_source(keychain):
    with pytest.raises(ConfigError, match="needs token_env or token_file"):
        resolve_server_token({"id": "local"})


@pytest.mark.parametrize("value", ["", "   ", 5])
def test_malformed_token_file_is_structural(keychain, value):
    with pytest.raises(ConfigError, match="token_file must be"):
        resolve_server_token({"id": "local", "token_file": value})


def test_config_loads_herdr_and_t3_servers_from_token_files(tmp_path, keychain):
    herdr = tmp_path / "herdr-token"
    herdr.write_text("herdr-secret")
    herdr.chmod(0o600)
    t3 = tmp_path / "t3-token"
    t3.write_text("t3-secret")
    t3.chmod(0o600)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[[servers]]\nid = "local"\nurl = "ws://x"\ntoken_file = "{herdr}"\n'
        f'[[servers]]\nid = "t3"\nurl = "http://127.0.0.1:3773"\nbackend = "t3"\n'
        f'token_env = "TOK"\ntoken_file = "{t3}"\n'
    )
    servers = resolve_profile(load_settings(cfg)).config.servers
    assert [(s.id, s.token) for s in servers] == [("local", "herdr-secret"), ("t3", "t3-secret")]


def test_structural_validation_does_not_need_the_token(tmp_path, keychain):
    raw = {"id": "local", "url": "ws://x", "token_file": str(tmp_path / "absent")}
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[[servers]]\nid = "local"\nurl = "ws://x"\ntoken_file = "{raw["token_file"]}"\n')
    snapshot = load_settings(cfg)
    with pytest.raises(TokenNotFoundError):
        resolve_profile(snapshot)
    with assume_tokens_present():
        assert resolve_profile(snapshot).config.servers[0].id == "local"


def test_config_service_writes_token_file_only_server_before_the_file_exists(tmp_path, keychain):
    from herdeck.deckapp.config_service import ConfigService

    service = ConfigService(tmp_path / "config.toml", tmp_path / "local.toml")
    payload = {
        "base": {
            "servers": [{"id": "local", "url": "ws://x", "token_file": str(tmp_path / "later")}]
        },
        "profiles": {},
        "local": {},
    }
    assert service.write(payload) == []
    assert "token_file" in (tmp_path / "config.toml").read_text()
