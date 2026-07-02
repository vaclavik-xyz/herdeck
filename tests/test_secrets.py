import herdeck.secrets as secrets


class FakeKeyring:
    """In-memory stand-in for the `keyring` module surface used by secrets.py."""

    def __init__(self):
        self.store = {}

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        if (service, name) in self.store:
            del self.store[(service, name)]
        else:
            raise KeyError(name)


def test_get_secret_prefers_env_over_keychain(monkeypatch):
    fake = FakeKeyring()
    fake.set_password("herdeck", "TOK", "from_keychain")
    monkeypatch.setattr(secrets, "_keyring", lambda: fake)
    monkeypatch.setenv("TOK", "from_env")
    assert secrets.get_secret("TOK") == "from_env"
    assert secrets.secret_source("TOK") == "env"


def test_get_secret_falls_back_to_keychain(monkeypatch):
    fake = FakeKeyring()
    fake.set_password("herdeck", "TOK", "from_keychain")
    monkeypatch.setattr(secrets, "_keyring", lambda: fake)
    monkeypatch.delenv("TOK", raising=False)
    assert secrets.get_secret("TOK") == "from_keychain"
    assert secrets.secret_source("TOK") == "keychain"
    assert secrets.has_secret("TOK") is True


def test_set_and_clear_secret(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(secrets, "_keyring", lambda: fake)
    monkeypatch.delenv("TOK", raising=False)
    secrets.set_secret("TOK", "v")
    assert fake.store[("herdeck", "TOK")] == "v"
    secrets.clear_secret("TOK")
    assert secrets.has_secret("TOK") is False
    assert secrets.secret_source("TOK") is None


def test_missing_keyring_backend_degrades_to_env_only(monkeypatch):
    def boom():
        raise RuntimeError("no backend")

    monkeypatch.setattr(secrets, "_keyring", boom)
    monkeypatch.delenv("TOK", raising=False)
    assert secrets.get_secret("TOK") is None  # never raises
    assert secrets.has_secret("TOK") is False


def test_build_notifier_resolves_telegram_token_via_secrets(monkeypatch):
    from herdeck.app import _build_notifier
    from herdeck.config import Config, Notifications, TelegramConfig

    fake = FakeKeyring()
    fake.set_password("herdeck", "TGTOK", "bot-token")
    monkeypatch.setattr(secrets, "_keyring", lambda: fake)
    monkeypatch.delenv("TGTOK", raising=False)  # env miss -> keychain fallback

    captured = []
    cfg = Config(
        servers=[],
        profiles={},
        overview_order=[],
        grid=(5, 3),
        notifications=Notifications(
            enabled=True,
            on=["blocked"],
            backends=["telegram"],
            telegram=TelegramConfig(token_env="TGTOK", chat_id="1"),
        ),
    )
    _build_notifier(
        cfg,
        telegram_factory=lambda tok, chat, thread: captured.append((tok, chat, thread)),
    )
    assert captured == [("bot-token", "1", None)]  # telegram token resolved via keychain


def test_doctor_check_config_sees_keychain_only_token(monkeypatch):
    from herdeck.doctor import check_config

    fake = FakeKeyring()
    fake.set_password("herdeck", "TOK", "v")
    monkeypatch.setattr(secrets, "_keyring", lambda: fake)
    monkeypatch.delenv("TOK", raising=False)
    chk = check_config("/cfg.toml", True, True, token_envs=("TOK",))
    assert chk.ok is True and "TOK=present" in chk.detail  # keychain token reported present


def test_doctor_check_notifications_sees_keychain_telegram(monkeypatch):
    from herdeck.config import Notifications, TelegramConfig
    from herdeck.doctor import check_notifications

    fake = FakeKeyring()
    fake.set_password("herdeck", "TGTOK", "v")
    monkeypatch.setattr(secrets, "_keyring", lambda: fake)
    monkeypatch.delenv("TGTOK", raising=False)
    n = Notifications(enabled=True, backends=["telegram"], telegram=TelegramConfig("TGTOK", "1"))
    assert "token_env=present" in check_notifications(n).detail


def test_doctor_read_config_facts_keychain_token_unmasks_real_error(tmp_path, monkeypatch):
    # A genuine RED test for the os.environ.get -> get_secret change at line ~199:
    # keychain-only token AND a non-token ConfigError (bad grid). The OLD code sees
    # the env var missing and suppresses the real error (err None); the FIXED code sees
    # the keychain token present and surfaces the invalid-config error.
    from herdeck.doctor import _read_config_facts

    fake = FakeKeyring()
    fake.set_password("herdeck", "TOK", "v")
    monkeypatch.setattr(secrets, "_keyring", lambda: fake)
    monkeypatch.delenv("TOK", raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[[servers]]\nid="local"\nurl="ws://x"\ntoken_env="TOK"\n[deck]\ngrid="bad"\n')
    has_servers, token_envs, err, _servers = _read_config_facts(str(cfg))
    assert token_envs == ["TOK"]
    assert err is not None and "invalid config" in err.detail  # real error not masked
