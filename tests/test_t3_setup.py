import argparse
import json
import tomllib

import pytest

from herdeck import t3_setup


def test_config_preserves_unknown_keys_and_inherited_selections():
    original = {"custom": {"keep": True}, "servers": [{"id": "herdr", "url": "ws://x", "token_env": "OLD"}],
        "deck": {"overview_order": ["herdr"]},
        "profiles": {"work": {"servers": ["herdr"], "custom": "keep"}}}
    result = t3_setup.updated_config(original, "t3", "http://127.0.0.1:3773", "NEW", "work")
    assert result["custom"] == {"keep": True}
    assert result["profiles"]["work"]["servers"] == ["herdr", "t3"]
    assert original["deck"]["overview_order"] == ["herdr"]
    assert result["deck"]["overview_order"] == ["herdr"]
    with pytest.raises(ValueError):
        t3_setup.updated_config(result, "t3", "http://127.0.0.1:3773", "NEW")


@pytest.mark.parametrize("fail_write", [False, True])
def test_setup_keychain_backup_and_rollback(tmp_path, monkeypatch, capsys, fail_write):
    path = tmp_path / "config.toml"
    path.write_text('[custom]\nkeep = true\n')
    local_path = tmp_path / "local.toml"
    local_path.write_text('[local]\nherdr_sessions = []\n')
    old = path.read_text()
    secret = "secret-never-in-output"
    secrets, revoked = {}, []
    monkeypatch.delenv("HERDECK_LOCAL_CONFIG", raising=False)
    monkeypatch.delenv("HERDECK_PROFILE", raising=False)
    monkeypatch.setattr(t3_setup, "get_secret", secrets.get)
    monkeypatch.setattr(t3_setup, "peek_keychain", secrets.get)
    monkeypatch.setattr(t3_setup, "set_secret", lambda k, v: secrets.update({k: v}))
    monkeypatch.setattr(t3_setup, "clear_secret", lambda k: secrets.pop(k, None))
    monkeypatch.setattr("herdeck.secrets.get_secret", secrets.get)
    monkeypatch.setattr(t3_setup.subprocess, "check_output", lambda *a, **kw:
        json.dumps({"token": secret, "sessionId": "session", "expiresAt": "later"}))
    monkeypatch.setattr(t3_setup.subprocess, "run", lambda *a, **kw: revoked.append(a))
    monkeypatch.setattr(t3_setup.T3Http, "get", lambda *a: {"threads": []})
    if fail_write:
        real = t3_setup.atomic_write
        monkeypatch.setattr(t3_setup, "atomic_write", lambda p, text:
            (_ for _ in ()).throw(OSError("full disk")) if p == local_path else real(p, text))
    args = argparse.Namespace(config=path, base_dir=tmp_path, url="http://127.0.0.1:3773", id="pilot", binary="t3")
    if fail_write:
        with pytest.raises(OSError):
            t3_setup.connect(args)
        assert path.read_text() == old and not secrets and revoked
    else:
        t3_setup.connect(args)
        assert tomllib.loads(path.read_text())["servers"][0]["backend"] == "t3"
        assert secrets and not revoked
    assert secret not in capsys.readouterr().out
    assert secret not in path.read_text()
    assert list(tmp_path.glob("config.toml.*.bak"))[0].read_text() == old


def test_disconnect_preserves_herdr_and_rollback_credential(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text('[[servers]]\nid="herdr"\nurl="ws://old"\n[[servers]]\nid="t3"\nbackend="t3"\n'
                    '[deck]\noverview_order=["herdr","t3"]\n[custom]\nkeep=true\n')
    t3_setup.disconnect(argparse.Namespace(config=path, id="t3"))
    data = tomllib.loads(path.read_text())
    assert data["servers"] == [{"id": "herdr", "url": "ws://old"}]
    assert data["custom"]["keep"]
    assert data["deck"]["overview_order"] == ["herdr"]
    assert json.loads(capsys.readouterr().out)["credential_retained"]
