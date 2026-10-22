import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from herdeck import t3_renew


@pytest.mark.parametrize("case", ["fresh", "renew", "verify_failure", "contract_failure", "restart_failure", "env_override", "missing_restart"])
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
        if route.endswith("environment"):
            return {"orchestrationProtocolVersion": 2, "capabilities": {}}
        if route.endswith("/session"):
            return dict(expiresAt="2099-01-01T00:00:00Z" if case == "fresh" else "2020-01-01T00:00:00Z")
        if case == "verify_failure":
            raise t3_renew.T3Error("Rejected", 401)
        return dict(threads=[]) if case == "contract_failure" else dict(threads=[], archivedThreads=[], projects=[])
    monkeypatch.setattr(t3_renew.T3Http, "get", get)
    def run(*a, **kw):
        if case == "restart_failure":
            raise OSError("launchd unavailable")
    monkeypatch.setattr(t3_renew.subprocess, "run", run)
    args = argparse.Namespace(config=path, id="t3", restart_label=None if case == "missing_restart" else "com.herdeck.app")
    if case == "env_override":
        monkeypatch.setenv("TEST_T3_TOKEN", "env-private")
    if case.endswith("failure") or case in ("env_override", "missing_restart"):
        with pytest.raises((t3_renew.T3Error, OSError, ValueError)):
            t3_renew.renew(args)
        assert secret["value"] == "old-private"
        assert calls == ([] if case in ("env_override", "missing_restart") else ["issue", "revoke"])
    else:
        t3_renew.renew(args)
        output = capsys.readouterr().out
        assert "private" not in output
        assert json.loads(output)["renewed"] == (case == "renew")
        assert secret["value"] == ("new-private" if case == "renew" else "old-private")
    assert path.read_text() == original


def _fake(tmp_path, version, exe=False, legacy=False):
    vdir = tmp_path / "runtime/versions" / version
    vdir.mkdir(parents=True)
    if exe:
        (vdir / "t3").write_text("#!/bin/sh\n")
    if legacy:
        (vdir / "node_modules/t3/dist").mkdir(parents=True)
        (vdir / "node_modules/t3/dist/bin.mjs").write_text("")


def test_cli_path_prefers_standalone_executable_for_prerelease(tmp_path):
    v = "0.0.46-preview.20261010.2909"
    _fake(tmp_path, v, exe=True, legacy=True)
    assert t3_renew._cli_path(tmp_path, v) == [str(tmp_path / "runtime/versions" / v / "t3")]


def test_cli_path_falls_back_to_node_bundle(tmp_path):
    _fake(tmp_path, "0.0.41", legacy=True)
    cli = t3_renew._cli_path(tmp_path, "0.0.41")
    assert cli[1].endswith("node_modules/t3/dist/bin.mjs") and len(cli) == 2


def test_cli_path_rejects_bad_version_and_missing_runtime(tmp_path):
    for bad in ("../evil", "1.2", "1.2.3-", "1.2.3/x", "1.2.3-a..b", "01.2.3", "1.2.3-01", "1.2.3+"):
        with pytest.raises(ValueError, match="Invalid runtime version"):
            t3_renew._cli_path(tmp_path, bad)
    with pytest.raises(ValueError, match="not found"):
        t3_renew._cli_path(tmp_path, "0.0.46")


def test_remote_script_compiles_and_matches_selection_and_scopes():
    compile(t3_renew._REMOTE, "<remote>", "exec")
    assert "orchestration:read" in t3_renew._REMOTE and "orchestration:operate" in t3_renew._REMOTE
    assert t3_renew._VERSION in t3_renew._REMOTE and '"t3"' in t3_renew._REMOTE


def test_local_issue_uses_runtime_cli_and_scopes(tmp_path, monkeypatch):
    v = "0.0.46-preview.20261010.2909"
    _fake(tmp_path, v, exe=True)
    (tmp_path / "runtime/service-state.json").write_text(json.dumps({"activeVersion": v}))
    seen = []
    class R:
        stdout = "{}"
    monkeypatch.setattr(t3_renew.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or R())
    t3_renew.issuer(argparse.Namespace(base_dir=tmp_path, binary=None, id="x", issuer_ssh=None), "issue")
    cmd = seen[0]
    assert cmd[0].endswith(f"{v}/t3")
    assert [cmd[i + 1] for i, a in enumerate(cmd) if a == "--scope"] == ["orchestration:read", "orchestration:operate"]


@pytest.mark.parametrize("v", ["0.1.2-nightly.20261022.3512", "0.0.46-nightly-2026", "0.0.46-preview.20261010.2909+build.1", "1.0.0"])
def test_cli_path_accepts_upstream_version_strings(tmp_path, v):
    _fake(tmp_path, v, exe=True)
    assert t3_renew._cli_path(tmp_path, v)[0].endswith("/t3")


def _issue_cmd(monkeypatch, tmp_path, binary):
    seen = []
    class R:
        stdout = "{}"
    monkeypatch.setattr(t3_renew.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or R())
    t3_renew.issuer(argparse.Namespace(base_dir=tmp_path, binary=binary, id="x", issuer_ssh=None), "issue")
    return seen[0]


def test_explicit_binary_wins_over_runtime_cli_in_renewal(tmp_path, monkeypatch):
    _fake(tmp_path, "0.0.46", exe=True)
    (tmp_path / "runtime/service-state.json").write_text(json.dumps({"activeVersion": "0.0.46"}))
    assert _issue_cmd(monkeypatch, tmp_path, "/opt/my/t3")[0] == "/opt/my/t3"


def test_renewal_falls_back_to_path_t3_without_runtime(tmp_path, monkeypatch):
    assert _issue_cmd(monkeypatch, tmp_path, None)[0] == "t3"


def _run_remote(home, version, operation="issue"):
    # The issuer script runs on the T3 host as `python3 -c`; execute it for real
    # against a fake ~/.t3 instead of only compiling it.
    (home / ".t3/runtime").mkdir(parents=True, exist_ok=True)
    (home / ".t3/runtime/service-state.json").write_text(json.dumps({"activeVersion": version}))
    payload = dict(operation=operation, label="Herdeck x", sessionId="sess-1")
    return subprocess.run([sys.executable, "-c", t3_renew._REMOTE], input=json.dumps(payload) + "\n",
                          capture_output=True, text=True, timeout=30, env={"HOME": str(home), "PATH": os.environ.get("PATH", "")})


def _fake_standalone(home, version):
    exe = home / ".t3/runtime/versions" / version / "t3"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\nprintf \x27{\"argv0\": \"%s\", \"args\": \"%s\"}\x27 \"$0\" \"$*\"\n")
    exe.chmod(0o755)
    return exe


@pytest.mark.parametrize("v", ["0.1.2-nightly.20261022.3512", "0.0.46"])
def test_remote_script_runs_standalone_t3_binary(tmp_path, v):
    exe = _fake_standalone(tmp_path, v)
    result = _run_remote(tmp_path, v)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["argv0"] == str(exe)
    assert out["args"] == ("auth session issue --json --scope orchestration:read --scope orchestration:operate"
                           f" --ttl 30d --label Herdeck x --base-dir {tmp_path / '.t3'}")
    revoke = _run_remote(tmp_path, v, "revoke")
    assert revoke.returncode == 0 and revoke.stdout == ""


def test_remote_script_falls_back_to_node_bundle(tmp_path):
    if not (Path("/opt/homebrew/bin/node").exists() or shutil.which("node")):
        pytest.skip("node not available to the remote script")
    bundle = tmp_path / ".t3/runtime/versions/0.0.41/node_modules/t3/dist/bin.mjs"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("console.log(JSON.stringify({args: process.argv.slice(2).join(' ')}))\n")
    result = _run_remote(tmp_path, "0.0.41")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["args"].startswith("auth session issue --json")


@pytest.mark.parametrize("v", ["../../evil", "0.1.2-nightly/../../x", "1.2"])
def test_remote_script_rejects_traversal_versions(tmp_path, v):
    result = _run_remote(tmp_path, v)
    assert result.returncode != 0 and "Invalid runtime version" in result.stderr


def test_remote_script_reports_missing_cli(tmp_path):
    result = _run_remote(tmp_path, "0.1.2-nightly.20261022.3512")
    assert result.returncode != 0 and "T3 runtime CLI not found" in result.stderr
