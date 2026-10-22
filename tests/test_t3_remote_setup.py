"""Remote T3 connect (--issuer-ssh), forward LaunchAgent, port choice and
--renew-all. No network, ssh, launchctl or Keychain: all injected."""
import argparse
import json
import plistlib
import tomllib

import pytest

from herdeck import t3_remote, t3_renew, t3_setup
from herdeck.t3_client import T3Error

V2 = {"orchestrationProtocolVersion": 2, "capabilities": {}}
SHELL = {"threads": [], "archivedThreads": [], "projects": []}
TARGET = "admin@100.115.61.124"


@pytest.mark.parametrize("ok", ["admin@100.115.61.124", "m4mini", "filip.v@m4-mini.tail1234.ts.net", "_svc@host",
                                "m4_mini", "1user@host_alias"])
def test_ssh_target_accepts_user_at_host(ok):
    assert t3_remote.validate_ssh_target(ok) == ok


@pytest.mark.parametrize("bad", [
    "-oProxyCommand=touch /tmp/x", "admin@-oProxyCommand=x", "admin@host;id", "admin@host id",
    "$(id)@host", "admin@host\n", "a@b@c", "admin@host:22", "admin@", "@host", "", "admin@::1",
    "admin@host.", "admin@.host", "-admin@host", "admin@`id`", "admin@-host", "admin@x-", None, 7,
])
def test_ssh_target_rejects_options_and_shell_text(bad):
    with pytest.raises(ValueError, match="Invalid SSH target"):
        t3_remote.validate_ssh_target(bad)


def test_issuer_rejects_injection_before_running_ssh(monkeypatch):
    monkeypatch.setattr(t3_renew.subprocess, "run", lambda *a, **kw: pytest.fail("ssh must not run"))
    args = argparse.Namespace(id="x", issuer_ssh="-oProxyCommand=sh", base_dir=None, binary=None)
    with pytest.raises(ValueError, match="Invalid SSH issuer"):
        t3_renew.issuer(args, "issue")


def test_pick_port_skips_configured_and_busy_ports(monkeypatch):
    data = {"servers": [{"id": "a", "url": "http://127.0.0.1:13773"},
                        {"id": "b", "url": "ws://localhost:13774"},
                        {"id": "c", "url": "ws://100.1.2.3:13776"}]}
    assert t3_remote.used_ports(data) == {13773, 13774}
    monkeypatch.setattr(t3_remote, "port_free", lambda port: port != 13775)
    assert t3_remote.pick_port(data) == 13776
    monkeypatch.setattr(t3_remote, "port_free", lambda port: False)
    with pytest.raises(ValueError, match="--local-port"):
        t3_remote.pick_port(data)


@pytest.mark.parametrize("state, expected", [
    ({"origin": "http://100.86.1.2:3773", "port": 3773}, ("100.86.1.2", 3773)),
    ({"origin": "http://[::1]:3773"}, ("[::1]", 3773)),
    ({"origin": "http://127.0.0.1:3773", "port": "3773; id"}, None),
    ({"port": 3773}, ("127.0.0.1", 3773)),
    ({}, None),
])
def test_remote_runtime_reads_origin_and_validates(monkeypatch, state, expected):
    out = json.dumps(state)
    monkeypatch.setattr(t3_remote.subprocess, "run", lambda cmd, **kw: type("R", (), {"stdout": out})())
    if expected is None:
        with pytest.raises(ValueError):
            t3_remote.remote_runtime(TARGET)
    else:
        assert t3_remote.remote_runtime(TARGET) == expected


def test_remote_runtime_script_reads_a_real_runtime_file(tmp_path):
    import subprocess
    import sys
    (tmp_path / ".t3/userdata").mkdir(parents=True)
    (tmp_path / ".t3/userdata/server-runtime.json").write_text(json.dumps({"origin": "http://127.0.0.1:3773", "port": 3773, "pid": 1}))
    result = subprocess.run([sys.executable, "-c", t3_remote._REMOTE_RUNTIME], capture_output=True, text=True,
                            timeout=30, env={"HOME": str(tmp_path)})
    assert json.loads(result.stdout) == {"origin": "http://127.0.0.1:3773", "host": None, "port": 3773}


def test_forward_plist_is_a_fixed_argv():
    plist = t3_remote.forward_plist("t3-m4mini", TARGET, 13774, 3773)
    assert plist["Label"] == "com.herdeck.t3-forward.t3-m4mini"
    assert plist["ProgramArguments"] == [
        "/usr/bin/ssh", "-N", "-T", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=8",
        "-L", "127.0.0.1:13774:127.0.0.1:3773", TARGET]
    assert plist["RunAtLoad"] and plist["KeepAlive"] and plist["ThrottleInterval"] == 10
    with pytest.raises(ValueError):
        t3_remote.forward_plist("x", "admin@h -R9:x:9", 1, 2)
    with pytest.raises(ValueError):
        t3_remote.forward_plist("x", TARGET, 0, 3773)


class Fake:
    """subprocess.run stand-in for ssh (runtime read + issuer) and launchctl."""

    def __init__(self, runtime=None, issue_ok=True):
        self.calls, self.issuer_ops = [], []
        self.runtime = runtime or {"origin": "http://127.0.0.1:3773", "port": 3773}
        self.issue_ok = issue_ok

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        out = ""
        if cmd[0] == "ssh":
            assert cmd[-2] == TARGET  # the target is one argv element, never shell text
            if "server-runtime.json" in cmd[-1]:
                out = json.dumps(self.runtime)
            else:
                op = json.loads(kw["input"])
                self.issuer_ops.append((op["operation"], op.get("sessionId")))
                if op["operation"] == "issue":
                    if not self.issue_ok:
                        raise t3_renew.subprocess.CalledProcessError(1, cmd, output="secret-leak")
                    out = json.dumps({"token": "remote-secret", "sessionId": "sess-r",
                                      "expiresAt": "2026-11-21T00:00:00Z"})
        return type("R", (), {"stdout": out, "returncode": 0})()

    def launchctl(self, verb):
        return [c for c in self.calls if c[0] == "launchctl" and c[1] == verb]


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[[servers]]\nid = "t3-local"\nurl = "http://127.0.0.1:13773"\n'
                    'token_env = "HERDECK_T3_T3_LOCAL_TOKEN"\nbackend = "t3"\n[custom]\nkeep = true\n')
    secrets = {"HERDECK_T3_T3_LOCAL_TOKEN": "local-secret"}
    monkeypatch.delenv("HERDECK_LOCAL_CONFIG", raising=False)
    monkeypatch.delenv("HERDECK_PROFILE", raising=False)
    monkeypatch.delenv("HERDECK_T3_T3_LOCAL_TOKEN", raising=False)
    monkeypatch.delenv("HERDECK_T3_T3_M4MINI_TOKEN", raising=False)
    for mod in (t3_setup, t3_renew):
        monkeypatch.setattr(mod, "peek_keychain", secrets.get)
        monkeypatch.setattr(mod, "set_secret", lambda k, v: secrets.update({k: v}))
    monkeypatch.setattr(t3_setup, "get_secret", secrets.get)
    monkeypatch.setattr(t3_setup, "clear_secret", lambda k: secrets.pop(k, None))
    monkeypatch.setattr("herdeck.secrets.get_secret", secrets.get)
    monkeypatch.setattr(t3_remote, "agents_dir", lambda: tmp_path / "LaunchAgents")
    monkeypatch.setattr(t3_remote, "logs_dir", lambda: tmp_path / "Logs")
    monkeypatch.setattr(t3_remote.sys, "platform", "darwin")
    monkeypatch.setattr(t3_remote, "port_free", lambda port: True)
    monkeypatch.setattr(t3_remote, "port_answers", lambda port, timeout=1.0: True)
    monkeypatch.setattr(t3_remote, "wait_for_port", lambda port, **kw: None)
    shell = {"value": SHELL}
    monkeypatch.setattr(t3_setup.T3Http, "get", lambda self, route:
                        V2 if route.endswith("environment") else shell["value"])
    fake = Fake()
    monkeypatch.setattr(t3_remote.subprocess, "run", fake)
    return argparse.Namespace(path=path, secrets=secrets, fake=fake, shell=shell, tmp=tmp_path)


def _args(env, **kw):
    base = dict(config=env.path, base_dir=env.tmp, binary=None, url=None, id="t3-m4mini",
                issuer_ssh=TARGET, local_port=None, remote_port=None, install_forward=True)
    return argparse.Namespace(**{**base, **kw})


def test_remote_connect_installs_forward_and_records_issuer(env, capsys):
    t3_setup.connect(_args(env))
    out = capsys.readouterr().out
    assert "secret" not in out
    result = json.loads(out)
    assert result["url"] == "http://127.0.0.1:13774"  # 13773 belongs to t3-local
    assert result["forward_agent"] == "com.herdeck.t3-forward.t3-m4mini"
    server = tomllib.loads(env.path.read_text())["servers"][1]
    assert server == {"id": "t3-m4mini", "url": "http://127.0.0.1:13774", "backend": "t3",
                      "token_env": "HERDECK_T3_T3_M4MINI_TOKEN", "issuer_ssh": TARGET}
    assert env.secrets["HERDECK_T3_T3_M4MINI_TOKEN"] == "remote-secret"
    plist = plistlib.loads((env.tmp / "LaunchAgents/com.herdeck.t3-forward.t3-m4mini.plist").read_bytes())
    assert plist["ProgramArguments"][-2:] == ["127.0.0.1:13774:127.0.0.1:3773", TARGET]
    assert len(env.fake.launchctl("bootstrap")) == 1
    assert env.fake.issuer_ops == [("issue", None)]


@pytest.mark.parametrize("runtime", [{"origin": "http://127.0.0.1:4001", "port": 4001},
                                     {"origin": "http://localhost:4001"},
                                     {"host": "0.0.0.0", "port": 4001}])
def test_remote_connect_forwards_the_reported_remote_port(env, capsys, runtime):
    env.fake.runtime = runtime
    t3_setup.connect(_args(env, local_port=14000))
    assert json.loads(capsys.readouterr().out)["url"] == "http://127.0.0.1:14000"
    plist = plistlib.loads((env.tmp / "LaunchAgents/com.herdeck.t3-forward.t3-m4mini.plist").read_bytes())
    assert "127.0.0.1:14000:127.0.0.1:4001" in plist["ProgramArguments"]


@pytest.mark.parametrize("failure", ["verify", "write"])
def test_remote_rollback_revokes_remotely_and_removes_forward(env, monkeypatch, failure):
    original = env.path.read_text()
    if failure == "verify":
        env.shell["value"] = {"threads": []}  # contract failure after issuing
    else:
        monkeypatch.setattr(t3_setup, "atomic_write", lambda p, text: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises((ValueError, OSError, T3Error)):
        t3_setup.connect(_args(env, remote_port=3773))
    assert env.path.read_text() == original
    assert "HERDECK_T3_T3_M4MINI_TOKEN" not in env.secrets
    assert env.fake.issuer_ops == [("issue", None), ("revoke", "sess-r")]
    assert not (env.tmp / "LaunchAgents/com.herdeck.t3-forward.t3-m4mini.plist").exists()
    assert len(env.fake.launchctl("bootout")) == 1


def test_remote_connect_with_token_file_skips_keychain(env, monkeypatch, capsys):
    def no_keychain(*a):
        raise AssertionError("Keychain must not be used")  # e.g. over SSH: PasswordSetError
    for mod in (t3_setup,):
        monkeypatch.setattr(mod, "set_secret", no_keychain)
        monkeypatch.setattr(mod, "peek_keychain", no_keychain)
        monkeypatch.setattr(mod, "get_secret", no_keychain)
    t3_setup.connect(_args(env, token_file=""))
    out = capsys.readouterr().out
    assert "secret" not in out
    token = env.tmp / "t3-m4mini-token"  # default: next to the config
    assert json.loads(out)["token_file"] == str(token)
    assert token.read_text() == "remote-secret\n" and (token.stat().st_mode & 0o777) == 0o600
    server = tomllib.loads(env.path.read_text())["servers"][1]
    assert server == {"id": "t3-m4mini", "url": "http://127.0.0.1:13774", "backend": "t3",
                      "issuer_ssh": TARGET, "token_file": str(token)}
    # The runtime and renewal both resolve this entry to the file.
    from herdeck.settings import resolve_server_token
    assert resolve_server_token(server) == ("remote-secret", "file")
    assert t3_renew._Credential(server).read() == "remote-secret"


def test_token_file_rollback_removes_the_file_and_revokes(env, tmp_path):
    original = env.path.read_text()
    env.shell["value"] = {"threads": []}  # contract failure after issuing
    token = tmp_path / "creds/m4"
    with pytest.raises((ValueError, T3Error)):
        t3_setup.connect(_args(env, remote_port=3773, token_file=str(token)))
    assert not token.exists() and env.path.read_text() == original
    assert env.fake.issuer_ops == [("issue", None), ("revoke", "sess-r")]


def test_token_file_refuses_an_existing_file_before_issuing(env, tmp_path):
    token = tmp_path / "taken"
    token.write_text("someone-else\n")
    with pytest.raises(ValueError, match="already exists"):
        t3_setup.connect(_args(env, remote_port=3773, token_file=str(token)))
    assert token.read_text() == "someone-else\n" and env.fake.issuer_ops == []


def test_remote_issue_failure_removes_forward_without_leaking(env):
    env.fake.issue_ok = False
    with pytest.raises(ValueError) as exc:
        t3_setup.connect(_args(env, remote_port=3773))
    assert "secret-leak" not in str(exc.value) and TARGET in str(exc.value)
    assert env.fake.issuer_ops == [("issue", None)]
    assert not (env.tmp / "LaunchAgents/com.herdeck.t3-forward.t3-m4mini.plist").exists()


def test_remote_connect_without_install_needs_a_live_tunnel(env, monkeypatch):
    with pytest.raises(ValueError, match="--local-port"):
        t3_setup.connect(_args(env, install_forward=False))
    with pytest.raises(ValueError, match="already used by another configured server"):
        t3_setup.connect(_args(env, install_forward=False, local_port=13773))
    monkeypatch.setattr(t3_remote, "port_answers", lambda port, timeout=1.0: False)
    with pytest.raises(ValueError, match="Nothing answers"):
        t3_setup.connect(_args(env, install_forward=False, local_port=13999))
    assert env.fake.calls == []


def test_remote_connect_with_existing_tunnel_installs_nothing(env, capsys):
    t3_setup.connect(_args(env, install_forward=False, local_port=13999))
    assert json.loads(capsys.readouterr().out)["url"] == "http://127.0.0.1:13999"
    assert not env.fake.launchctl("bootstrap") and not (env.tmp / "LaunchAgents").exists()


def test_remote_connect_refuses_invalid_target_before_side_effects(env):
    with pytest.raises(ValueError, match="Invalid SSH target"):
        t3_setup.connect(_args(env, issuer_ssh="admin@host -oProxyCommand=x"))
    assert env.fake.calls == []


def test_disconnect_removes_the_forward_it_installed(env, capsys):
    t3_setup.connect(_args(env, remote_port=3773))
    capsys.readouterr()
    t3_setup.disconnect(argparse.Namespace(config=env.path, id="t3-m4mini"))
    assert json.loads(capsys.readouterr().out)["forward_agent_removed"] is True
    assert not (env.tmp / "LaunchAgents/com.herdeck.t3-forward.t3-m4mini.plist").exists()
    assert [s["id"] for s in tomllib.loads(env.path.read_text())["servers"]] == ["t3-local"]
    t3_setup.disconnect(argparse.Namespace(config=env.path, id="t3-local"))
    assert json.loads(capsys.readouterr().out)["forward_agent_removed"] is False


def test_disconnect_reports_a_forward_it_could_not_remove(env, monkeypatch, capsys):
    t3_setup.connect(_args(env, remote_port=3773))
    capsys.readouterr()
    monkeypatch.setattr(t3_remote, "remove_forward", lambda sid: (_ for _ in ()).throw(OSError("launchd")))
    t3_setup.disconnect(argparse.Namespace(config=env.path, id="t3-m4mini"))
    out = json.loads(capsys.readouterr().out)
    assert out["forward_agent_removed"] is False and "com.herdeck.t3-forward.t3-m4mini" in out["warning"]
    assert [s["id"] for s in tomllib.loads(env.path.read_text())["servers"]] == ["t3-local"]


# --- renew-all ---------------------------------------------------------------

def _renew_env(tmp_path, monkeypatch, sessions, fail=()):
    path = tmp_path / "config.toml"
    servers = "".join(
        f'[[servers]]\nid = "{sid}"\nurl = "http://127.0.0.1:{port}"\nbackend = "t3"\n'
        + (f'token_env = "TOK_{port}"\n' if sid != "filed" else f'token_file = "{tmp_path / "filed-token"}"\n')
        + (f'issuer_ssh = "{TARGET}"\n' if sid == "remote" else "")
        for sid, port in (("remote", 13774), ("local", 13773), ("fresh", 13775), ("filed", 13776)))
    path.write_text('[[servers]]\nid = "herdr"\nurl = "ws://x"\ntoken_env = "H"\n' + servers)
    secrets = {f"TOK_{p}": f"old-{p}" for p in (13773, 13774, 13775)}
    filed = tmp_path / "filed-token"
    filed.write_text("old-13776\n")
    filed.chmod(0o600)
    monkeypatch.setattr(t3_renew, "peek_keychain", secrets.get)
    monkeypatch.setattr(t3_renew, "set_secret", lambda k, v: secrets.update({k: v}))
    issued = []

    def issuer(args, operation, session_id=None):
        issued.append((args.id, args.issuer_ssh, operation))
        return dict(token=f"new-{args.id}", sessionId=f"s-{args.id}", expiresAt="2026-11-21T00:00:00Z")
    monkeypatch.setattr(t3_renew, "issuer", issuer)

    def get(http, route):
        if route.endswith("/session"):
            return {"expiresAt": sessions[http.url]}
        if route.endswith("environment"):
            return V2
        if any(f"new-{sid}" == http._token for sid in fail):
            raise t3_renew.T3Error("Rejected", 401)
        return SHELL
    monkeypatch.setattr(t3_renew.T3Http, "get", get)
    restarts = []
    monkeypatch.setattr(t3_renew.subprocess, "run", lambda cmd, **kw: restarts.append(cmd))
    return path, secrets, issued, restarts


def _sessions():
    soon, later = "2020-01-01T00:00:00Z", "2099-01-01T00:00:00Z"
    return {f"http://127.0.0.1:{p}": (later if p == 13775 else soon) for p in (13773, 13774, 13775, 13776)}


def test_renew_all_mixed_results_restart_once_and_exit_nonzero(tmp_path, monkeypatch, capsys):
    path, secrets, issued, restarts = _renew_env(tmp_path, monkeypatch, _sessions(), fail=("local",))
    args = argparse.Namespace(config=path, restart_label="dev.herdeck.runtime", issuer_ssh=None,
                              base_dir=tmp_path, binary=None)
    assert t3_renew.renew_all(args) is False
    out = capsys.readouterr().out
    assert "new-" not in out and "old-" not in out
    lines = [json.loads(line) for line in out.splitlines()]
    by_id = {r["server_id"]: r for r in lines[:-1]}
    assert by_id["remote"]["renewed"] is True
    assert by_id["local"]["renewed"] is False and "error" in by_id["local"]
    assert by_id["fresh"] == {"server_id": "fresh", "renewed": False, "expires_at": "2099-01-01T00:00:00Z"}
    assert by_id["filed"]["renewed"] is True and "herdr" not in by_id
    assert lines[-1]["summary"] == {"servers": 4, "renewed": ["remote", "filed"], "ok": False, "runtime_restarted": True}
    # Remote via its recorded issuer_ssh, local via the local CLI; the failure revoked its own session.
    assert issued == [("remote", TARGET, "issue"), ("local", None, "issue"), ("local", None, "revoke"),
                      ("filed", None, "issue")]
    assert secrets == {"TOK_13773": "old-13773", "TOK_13774": "new-remote", "TOK_13775": "old-13775"}
    # A token_file server is renewed in its file (what the runtime reads first), owner-only.
    filed = tmp_path / "filed-token"
    assert filed.read_text() == "new-filed\n" and (filed.stat().st_mode & 0o777) == 0o600
    assert len(restarts) == 1 and restarts[0][-1].endswith("/dev.herdeck.runtime")


def test_renew_all_restarts_nothing_when_nothing_was_due(tmp_path, monkeypatch, capsys):
    later = {k: "2099-01-01T00:00:00Z" for k in _sessions()}
    path, secrets, issued, restarts = _renew_env(tmp_path, monkeypatch, later)
    args = argparse.Namespace(config=path, restart_label="dev.herdeck.runtime", issuer_ssh=None,
                              base_dir=tmp_path, binary=None)
    assert t3_renew.renew_all(args) is True
    assert not issued and not restarts
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["summary"]["runtime_restarted"] is False


def test_renew_all_restart_failure_is_reported_and_keeps_new_credentials(tmp_path, monkeypatch, capsys):
    path, secrets, issued, restarts = _renew_env(tmp_path, monkeypatch, _sessions())

    def boom(cmd, **kw):
        raise OSError("launchd unavailable")
    monkeypatch.setattr(t3_renew.subprocess, "run", boom)
    args = argparse.Namespace(config=path, restart_label="dev.herdeck.runtime", issuer_ssh=None,
                              base_dir=tmp_path, binary=None)
    assert t3_renew.renew_all(args) is False
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])["summary"]
    assert summary["renewed"] == ["remote", "local", "filed"] and summary["restart_error"] == "OSError"
    assert secrets["TOK_13773"] == "new-local" and secrets["TOK_13774"] == "new-remote"


def test_renew_all_refuses_cli_issuer_and_bad_label(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("")
    with pytest.raises(ValueError, match="drop --issuer-ssh"):
        t3_renew.renew_all(argparse.Namespace(config=path, restart_label=None, issuer_ssh=TARGET))
    with pytest.raises(ValueError, match="Invalid runtime launchd label"):
        t3_renew.renew_all(argparse.Namespace(config=path, restart_label="x;y", issuer_ssh=None))


def test_single_renew_falls_back_to_recorded_issuer(tmp_path, monkeypatch, capsys):
    path, secrets, issued, restarts = _renew_env(tmp_path, monkeypatch, _sessions())
    args = argparse.Namespace(config=path, id="remote", restart_label="dev.herdeck.runtime",
                              issuer_ssh=None, base_dir=tmp_path, binary=None)
    t3_renew.renew(args)
    assert issued == [("remote", TARGET, "issue")]
    assert json.loads(capsys.readouterr().out)["runtime_restarted"] is True and len(restarts) == 1


def test_renew_agent_plist_runs_renew_all_hourly(tmp_path):
    plist = t3_remote.renew_plist(tmp_path / "config.toml", "dev.herdeck.runtime")
    assert plist["Label"] == "com.herdeck.t3-renew" and plist["StartInterval"] == 3600
    assert plist["ProgramArguments"][1:] == ["-m", "herdeck.t3_setup", "--renew-all", "--config",
                                             str(tmp_path / "config.toml"), "--restart-label", "dev.herdeck.runtime"]
    assert (t3_remote.Path(plist["EnvironmentVariables"]["PYTHONPATH"]) / "herdeck/t3_setup.py").is_file()
    with pytest.raises(ValueError):
        t3_remote.renew_plist(tmp_path / "c.toml", "a b")


@pytest.mark.parametrize("ok", [True, False])
def test_main_renew_all_exit_status(monkeypatch, tmp_path, ok):
    monkeypatch.setattr(t3_renew, "renew_all", lambda args: ok)
    monkeypatch.setattr(t3_setup.sys, "argv", ["t3_setup", "--renew-all", "--config", str(tmp_path / "c.toml")])
    if ok:
        t3_setup.main()
    else:
        with pytest.raises(SystemExit) as exc:
            t3_setup.main()
        assert exc.value.code == 1


def test_main_rejects_two_modes(monkeypatch, capsys):
    monkeypatch.setattr(t3_setup.sys, "argv", ["t3_setup", "--renew", "--renew-all"])
    with pytest.raises(SystemExit) as exc:
        t3_setup.main()
    assert exc.value.code == 1 and "Choose one of" in capsys.readouterr().err
