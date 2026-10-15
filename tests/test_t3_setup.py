import argparse
import json
import subprocess
import tomllib

import pytest

from herdeck import t3_setup
from herdeck.t3 import T3Error


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


V2 = {"orchestrationProtocolVersion": 2, "capabilities": {}}


@pytest.mark.parametrize("fail_write", [False, True, "contract"])
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
    issue_args = []
    def issue(cmd, **kw):
        issue_args.append(cmd)
        return json.dumps({"token": secret, "sessionId": "session", "expiresAt": "later"})
    monkeypatch.setattr(t3_setup.subprocess, "check_output", issue)
    monkeypatch.setattr(t3_setup.subprocess, "run", lambda *a, **kw: revoked.append(a))
    monkeypatch.setattr(t3_setup.T3Http, "get", lambda self, route: V2 if route.endswith("environment") else (
        {"threads": []} if fail_write == "contract" else {"threads": [], "archivedThreads": [], "projects": []}))
    if fail_write:
        real = t3_setup.atomic_write
        monkeypatch.setattr(t3_setup, "atomic_write", lambda p, text:
            (_ for _ in ()).throw(OSError("full disk")) if p == local_path else real(p, text))
    args = argparse.Namespace(config=path, base_dir=tmp_path, url="http://127.0.0.1:3773", id="pilot", binary="t3")
    assert True
    if fail_write:
        with pytest.raises((OSError, T3Error)):
            t3_setup.connect(args)
        assert path.read_text() == old and not secrets and revoked
    else:
        t3_setup.connect(args)
        assert tomllib.loads(path.read_text())["servers"][0]["backend"] == "t3"
        assert secrets and not revoked
    cmd = issue_args[0]
    pairs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--scope"]
    assert pairs == ["orchestration:read", "orchestration:operate"]
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


def test_setup_rolls_back_on_protocol_1_server(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text("[custom]\nkeep = true\n")
    old = path.read_text()
    secrets, revoked = {}, []
    monkeypatch.delenv("HERDECK_LOCAL_CONFIG", raising=False)
    monkeypatch.delenv("HERDECK_PROFILE", raising=False)
    monkeypatch.setattr(t3_setup, "get_secret", secrets.get)
    monkeypatch.setattr(t3_setup, "peek_keychain", secrets.get)
    monkeypatch.setattr(t3_setup, "set_secret", lambda k, v: secrets.update({k: v}))
    monkeypatch.setattr(t3_setup, "clear_secret", lambda k: secrets.pop(k, None))
    monkeypatch.setattr("herdeck.secrets.get_secret", secrets.get)
    monkeypatch.setattr(t3_setup.subprocess, "check_output", lambda *a, **kw:
        json.dumps({"token": "s", "sessionId": "session", "expiresAt": "later"}))
    monkeypatch.setattr(t3_setup.subprocess, "run", lambda *a, **kw: revoked.append(a))
    monkeypatch.setattr(t3_setup.T3Http, "get", lambda self, route: {"environmentId": "x"})
    args = argparse.Namespace(config=path, base_dir=tmp_path, url="http://127.0.0.1:3773", id="pilot", binary="t3")
    with pytest.raises(ValueError, match=r"protocol 1; Herdeck needs 2 \(T3 0.0.46\+\)"):
        t3_setup.connect(args)
    assert path.read_text() == old and not secrets and revoked


def _setup_env(tmp_path, monkeypatch, check_output):
    path = tmp_path / "config.toml"
    path.write_text("[custom]\nkeep = true\n")
    secrets, revoked = {}, []
    monkeypatch.delenv("HERDECK_LOCAL_CONFIG", raising=False)
    monkeypatch.delenv("HERDECK_PROFILE", raising=False)
    monkeypatch.setattr(t3_setup, "get_secret", secrets.get)
    monkeypatch.setattr(t3_setup, "peek_keychain", secrets.get)
    monkeypatch.setattr(t3_setup, "set_secret", lambda k, v: secrets.update({k: v}))
    monkeypatch.setattr(t3_setup, "clear_secret", lambda k: secrets.pop(k, None))
    monkeypatch.setattr("herdeck.secrets.get_secret", secrets.get)
    monkeypatch.setattr(t3_setup.subprocess, "check_output", check_output)
    monkeypatch.setattr(t3_setup.subprocess, "run", lambda cmd, **kw: revoked.append(cmd))
    monkeypatch.setattr(t3_setup.T3Http, "get", lambda self, route: V2 if route.endswith("environment") else
                        {"threads": [], "archivedThreads": [], "projects": []})
    return path, secrets, revoked


def _layout(tmp_path):
    v = "0.0.46"
    d = tmp_path / "runtime/versions" / v
    d.mkdir(parents=True)
    (d / "t3").write_text("")
    (tmp_path / "runtime/service-state.json").write_text(json.dumps({"activeVersion": v}))
    return str(d / "t3")


@pytest.mark.parametrize("mode", ["explicit", "runtime", "path"])
def test_setup_resolves_cli_for_issue_and_revoke(tmp_path, monkeypatch, mode):
    issued = []
    def out(cmd, **kw):
        issued.append(cmd)
        return json.dumps({"token": "tok", "sessionId": "sid", "expiresAt": "later"})
    path, secrets, revoked = _setup_env(tmp_path, monkeypatch, out)
    runtime = _layout(tmp_path) if mode != "path" else None
    binary = "/opt/my/t3" if mode == "explicit" else None
    expected = {"explicit": "/opt/my/t3", "runtime": runtime, "path": "t3"}[mode]
    monkeypatch.setattr(t3_setup, "atomic_write", lambda p, text: (_ for _ in ()).throw(OSError("full")))
    args = argparse.Namespace(config=path, base_dir=tmp_path, url="http://127.0.0.1:3773", id="pilot", binary=binary)
    with pytest.raises(OSError):
        t3_setup.connect(args)
    assert issued[0][0] == expected
    assert revoked and revoked[0][0] == expected


def test_failed_issue_gives_clear_message_and_writes_nothing(tmp_path, monkeypatch):
    def out(cmd, **kw):
        raise subprocess.CalledProcessError(2, cmd, output="SECRET-TOKEN-LEAK")
    path, secrets, revoked = _setup_env(tmp_path, monkeypatch, out)
    old = path.read_text()
    args = argparse.Namespace(config=path, base_dir=tmp_path, url="http://127.0.0.1:3773", id="pilot", binary="/opt/old/t3")
    with pytest.raises(ValueError) as e:
        t3_setup.connect(args)
    msg = str(e.value)
    assert "/opt/old/t3" in msg and "0.0.46+" in msg and "SECRET-TOKEN-LEAK" not in msg
    assert path.read_text() == old and not secrets and not revoked
