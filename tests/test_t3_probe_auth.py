import json

import pytest

from tests.t3_probe_auth import session_token


@pytest.mark.parametrize("fail", [False, True])
def test_probe_revokes_credential_on_success_and_failure(monkeypatch, fail):
    calls = []
    monkeypatch.setattr("tests.t3_probe_auth.subprocess.check_output", lambda *a, **kw:
        json.dumps({"token": "private", "sessionId": "id-1"}))
    class Result:
        returncode = 0
    monkeypatch.setattr("tests.t3_probe_auth.subprocess.run", lambda cmd, **kw: calls.append(cmd) or Result())
    def run():
        with session_token("t3", "/tmp/pilot") as token:
            assert token == "private"
            if fail:
                raise ValueError("probe failed")
    if fail:
        with pytest.raises(ValueError):
            run()
    else:
        run()
    assert calls == [["t3", "auth", "session", "revoke", "id-1", "--base-dir", "/tmp/pilot"]]
