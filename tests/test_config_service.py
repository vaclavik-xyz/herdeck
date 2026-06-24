import herdeck.secrets as secret_store
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
