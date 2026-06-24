import herdeck.secrets as secrets
from herdeck.deckapp.config_service import ConfigService

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
    svc = _svc(tmp_path)
    data = svc.read()
    # No secret VALUE appears anywhere in the payload.
    assert "real" not in repr(data)
    assert data["secrets"]["TOK"] == {"set": True, "source": "env"}
    assert data["secrets"]["TG"] == {"set": False, "source": None}


def test_read_surfaces_profile_only_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("PTG", raising=False)
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
