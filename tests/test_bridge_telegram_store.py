import json
import stat

import pytest

from herdeck.bridge_telegram import (
    BridgeTelegramStore,
    TelegramSettings,
    default_paths,
    parse_telegram,
    to_raw_telegram,
)
from herdeck.config import ConfigError

TOKEN = "123456789:" + "A" * 35


def mk(tmp_path, env=None):
    return BridgeTelegramStore(
        tmp_path / "tg.toml", tmp_path / "tok", clock=lambda: 100.0, env={} if env is None else env
    )


def mode(p):
    return stat.S_IMODE(p.stat().st_mode)


def test_defaults_when_unset(tmp_path):
    s = mk(tmp_path)
    assert s.revision == 0 and s.raw is None and s.error is None
    assert s.settings == TelegramSettings()
    d = s.settings
    assert (d.enabled, d.chat_id, d.message_thread_id, d.interactive) == (False, "", None, False)
    assert (d.allowed_user_ids, d.prompt_max_chars, d.only_when_away) == ([], 1200, 0)
    assert (d.language, d.sound) == ("en", True)


def test_put_ok_and_reload(tmp_path):
    s = mk(tmp_path)
    raw = {"enabled": True, "chat_id": "-100123", "interactive": True,
           "allowed_user_ids": [5], "only_when_away": 10, "language": "cs"}
    r = s.put(0, raw, "alice")
    assert r.ok and r.revision == 1
    assert mode(s.path) == 0o600
    s2 = mk(tmp_path)
    assert s2.revision == 1 and s2.updated_by == "alice" and s2.updated_at_ms == 100000
    assert s2.raw == s.raw and s2.settings == s.settings
    assert s2.settings.chat_id == "-100123" and s2.settings.language == "cs"


@pytest.mark.parametrize("raw", [
    {"bogus": 1},
    {"prompt_max_chars": True},
    {"prompt_max_chars": 100},
    {"prompt_max_chars": 5000},
    {"only_when_away": -1},
    {"only_when_away": 1441},
    {"language": "de"},
    {"interactive": True},
    {"interactive": True, "allowed_user_ids": []},
    {"chat_id": "abc"},
    {"chat_id": "@abc"},
    {"enabled": "yes"},
    {"allowed_user_ids": [True]},
    {"message_thread_id": True},
    "notadict",
])
def test_put_invalid(tmp_path, raw):
    s = mk(tmp_path)
    assert s.put(0, {"chat_id": "42"}, "x").ok
    before = s.path.read_bytes()
    r = s.put(1, raw, "x")
    assert not r.ok and r.error == "invalid" and r.revision == 1
    assert s.path.read_bytes() == before and s.revision == 1


def test_valid_chat_ids(tmp_path):
    for cid in ("-1001234", "42", "@my_chan"):
        assert parse_telegram({"chat_id": cid}).chat_id == cid


def test_stale_and_too_large(tmp_path):
    s = mk(tmp_path)
    r = s.put(3, {}, "x")
    assert r.error == "stale_revision" and not s.path.exists()
    r = s.put(0, {"allowed_user_ids": list(range(20000))}, "x")
    assert r.error == "too_large" and not s.path.exists()


def test_roundtrip_to_raw():
    s = parse_telegram({"enabled": True, "message_thread_id": 7})
    assert parse_telegram(to_raw_telegram(s)) == s


def test_token_set_clear(tmp_path):
    s = mk(tmp_path)
    assert s.token() is None and s.token_source() is None
    assert s.set_token(TOKEN) is None
    assert s.token_path.read_text() == TOKEN and mode(s.token_path) == 0o600
    assert s.token() == TOKEN and s.token_source() == "file"
    assert s.clear_token() is None
    assert not s.token_path.exists() and s.token_source() is None
    assert s.clear_token() is None


@pytest.mark.parametrize("bad", ["", "abc", "1234:short", "x" * 40, 5, None, TOKEN + "\n", "1234:" + "A" * 65])
def test_token_invalid_leaves_file(tmp_path, bad):
    s = mk(tmp_path)
    s.set_token(TOKEN)
    assert s.set_token(bad) == "invalid"
    assert s.token_path.read_text() == TOKEN


def test_token_env_wins(tmp_path):
    env_tok = "987654321:" + "B" * 35
    s = mk(tmp_path, {"HERDECK_TELEGRAM_TOKEN": env_tok})
    assert s.token() == env_tok and s.token_source() == "env"
    assert s.set_token(TOKEN) == "env_locked"
    assert s.clear_token() == "env_locked"
    assert not s.token_path.exists()


def test_frame_never_has_token(tmp_path):
    s = mk(tmp_path)
    s.set_token(TOKEN)
    s.put(0, {"chat_id": "42"}, "x")
    f = s.frame("srv", {"active": True, "token_source": s.token_source()})
    assert f["type"] == "telegram" and f["server_id"] == "srv" and f["revision"] == 1
    assert f["status"]["active"] is True and f["settings"]["chat_id"] == "42"
    assert TOKEN not in json.dumps(f)
    assert mk(tmp_path / "fresh").frame("s", {})["settings"] is None


def test_bad_token_not_in_error(tmp_path):
    s = mk(tmp_path)
    secret = "1234:" + "Z" * 100
    assert s.set_token(secret) == "invalid"
    r = s.put(0, {"chat_id": secret}, "x")
    assert secret not in json.dumps(r.__dict__)


def test_corrupt_doc_served_unset(tmp_path):
    p = tmp_path / "tg.toml"
    p.write_text("revision = 1\nbogus = 2\n")
    s = mk(tmp_path)
    assert s.error and s.revision == 0 and s.settings == TelegramSettings()


def test_default_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HERDECK_BRIDGE_TELEGRAM", raising=False)
    monkeypatch.delenv("HERDECK_TELEGRAM_TOKEN_FILE", raising=False)
    doc, tok = default_paths()
    assert doc.name == "bridge-telegram.toml" and tok.name == "telegram-token"
    doc, tok = default_paths("a/b")
    assert doc.name == "local-bridge-telegram-a_b.toml"
    monkeypatch.setenv("HERDECK_BRIDGE_TELEGRAM", "/x/d.toml")
    monkeypatch.setenv("HERDECK_TELEGRAM_TOKEN_FILE", "/x/t")
    assert default_paths() == (type(doc)("/x/d.toml"), type(doc)("/x/t"))


def test_parse_error_type():
    with pytest.raises(ConfigError):
        parse_telegram({"nope": 1})


def test_token_non_ascii_digits_invalid(tmp_path):
    s = mk(tmp_path)
    assert s.set_token("١٢٣٤٥:" + "A" * 35) == "invalid"
    assert not s.token_path.exists()
    s.set_token(TOKEN)
    assert s.set_token("١٢٣٤٥:" + "A" * 35) == "invalid"
    assert s.token_path.read_text() == TOKEN


def test_chat_id_non_ascii_digits_invalid():
    with pytest.raises(ConfigError):
        parse_telegram({"chat_id": "١٢٣"})


def test_garbage_token_ignored(tmp_path):
    s = mk(tmp_path)
    s.token_path.write_text("garbage")
    assert s.token() is None and s.token_source() is None
    s2 = mk(tmp_path, {"HERDECK_TELEGRAM_TOKEN": "junk"})
    assert s2.token() is None and s2.token_source() is None
    assert s2.set_token(TOKEN) is None


BAD_ENV = "12345678:short-typo-secretish"
BAD_FILE = "98765432:another-typo-value"


def _tg_warnings(caplog):
    return [r for r in caplog.records
            if r.name == "herdeck.bridge_telegram" and r.levelname == "WARNING"]


def test_invalid_env_token_warned_once_without_value(tmp_path, caplog):
    caplog.set_level("WARNING", logger="herdeck.bridge_telegram")
    s = mk(tmp_path, {"HERDECK_TELEGRAM_TOKEN": BAD_ENV})
    for _ in range(5):
        assert s.token() is None
        s.token_source()
    warned = _tg_warnings(caplog)
    assert len(warned) == 1
    assert "HERDECK_TELEGRAM_TOKEN" in warned[0].getMessage()
    for part in (BAD_ENV, "12345678", "short-typo-secretish", "typo"):
        assert part not in caplog.text


def test_invalid_token_file_warned_once_and_again_after_a_fix(tmp_path, caplog):
    caplog.set_level("WARNING", logger="herdeck.bridge_telegram")
    s = mk(tmp_path)
    s.token_path.write_text(BAD_FILE)
    for _ in range(5):
        assert s.token() is None
    assert len(_tg_warnings(caplog)) == 1
    s.token_path.write_text(TOKEN)
    assert s.token() == TOKEN
    s.token_path.write_text(BAD_FILE)
    assert s.token() is None
    assert s.token() is None
    assert len(_tg_warnings(caplog)) == 2  # a new typo is news again
    for part in (BAD_FILE, "98765432", "another-typo-value", TOKEN):
        assert part not in caplog.text


def test_missing_or_empty_token_not_warned(tmp_path, caplog):
    caplog.set_level("WARNING", logger="herdeck.bridge_telegram")
    s = mk(tmp_path, {"HERDECK_TELEGRAM_TOKEN": "  "})
    assert s.token() is None
    s.token_path.write_text("\n")
    assert s.token() is None
    assert _tg_warnings(caplog) == []
