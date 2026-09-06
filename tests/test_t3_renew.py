import argparse
import json

import pytest

from herdeck import t3_renew


@pytest.mark.parametrize("case", ["fresh", "renew", "verify_failure", "restart_failure", "env_override"])
def test_renew_preserves_identity_and_old_access_on_failure(tmp_path, monkeypatch, capsys, case):
    path = tmp_path / "config.toml"
    original = '[[servers]]\nid="t3"\nbackend="t3"\nurl="http://127.0.0.1:3773"\ntoken_env="TEST_T3_TOKEN"\n'
    path.write_text(original)
    secret = {"value": "old-private"}
    calls = []
    monkeypatch.delenv("TEST_T3_TOKEN", raising=False)
    monkeypatch.setattr(t3_renew, "peek_keychain", lambda name: secret["value"])
    monkeypatch.setattr(t3_renew, "set_secret", lambda name, value: secret.update(value=value))
    def issue(args, operation, session_id=None):
        calls.append(operation)
        return dict(token="new-private", sessionId="new-id", expiresAt="2026-10-06T12:00:00Z")
    monkeypatch.setattr(t3_renew, "issuer", issue)
    def get(http, route):
        if route.endswith("/session"):
            return dict(expiresAt="2099-01-01T00:00:00Z" if case == "fresh" else "2020-01-01T00:00:00Z")
        if case == "verify_failure":
            raise t3_renew.T3Error("Rejected", 401)
        return dict(threads=[])
    monkeypatch.setattr(t3_renew.T3Http, "get", get)
    def run(*a, **kw):
        if case == "restart_failure":
            raise OSError("launchd unavailable")
    monkeypatch.setattr(t3_renew.subprocess, "run", run)
    args = argparse.Namespace(config=path, id="t3", restart_label="com.herdeck.app")
    if case == "env_override":
        monkeypatch.setenv("TEST_T3_TOKEN", "env-private")
    if case.endswith("failure") or case == "env_override":
        with pytest.raises((t3_renew.T3Error, OSError, ValueError)):
            t3_renew.renew(args)
        assert secret["value"] == "old-private"
        assert calls == ([] if case == "env_override" else ["issue", "revoke"])
    else:
        t3_renew.renew(args)
        output = capsys.readouterr().out
        assert "private" not in output
        assert json.loads(output)["renewed"] == (case == "renew")
        assert secret["value"] == ("new-private" if case == "renew" else "old-private")
    assert path.read_text() == original
