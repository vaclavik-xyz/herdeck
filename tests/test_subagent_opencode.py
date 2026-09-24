"""OpenCode subagent tracking: the plugin's payloads in herdeck-subagent-hook,
the plugin itself (run under node when available) and its install matrix.

Every test works in a temp HOME: the real ~/.config/opencode is never read or
written."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from herdeck import hooks_install as hi
from herdeck import subagent_hook as hook

PANE = "w1-p7"
HOOK = "/opt/herdeck/venv/bin/herdeck-subagent-hook"
HERDR_PLUGIN = "// installed by herdr\nexport default { id: 'herdr.opencode' };\n"


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, pane, token, timeout_s):
        self.calls.append((pane, token))


@pytest.fixture
def env(tmp_path):
    return {"HERDR_PANE_ID": PANE, "HERDECK_SUBAGENT_SPOOL_DIR": str(tmp_path / "spool")}


def oc(event, child="ses_child1", parent="ses_root", root="ses_root", **kw):
    payload = {
        "hook_event_name": event,
        "session_id": child,
        "parent_id": parent,
        "root_session_id": root,
        "depth": 1,
        "title": "Review the diff (@reviewer subagent)",
    }
    payload.update(kw)
    return payload


def fire(payload, env, rec, now=1_000_000_000_000):
    hook.run(json.dumps(payload).encode(), ["--provider", "opencode"], env, reporter=rec, now_ms=lambda: now)


def entries(env):
    with open(hook.spool_path(PANE, env), encoding="utf-8") as fh:
        return json.load(fh)["entries"]


# --- payloads -------------------------------------------------------------------


def test_opencode_is_an_explicit_provider():
    assert hook.detect_provider(oc("session.created"), "opencode") == "opencode"


def test_child_session_lifecycle(env):
    rec = Recorder()
    fire(oc("session.created"), env, rec)
    (entry,) = entries(env)
    assert entry["provider"] == "opencode"
    assert entry["status"] == "running"
    assert entry["description"] == "Review the diff"
    assert entry["type"] == "reviewer"
    assert entry["depth"] == 1
    fire(oc("session.status", status="busy"), env, rec, now=1_000_000_060_000)
    assert entries(env)[0]["last_seen_ms"] == 1_000_000_060_000
    fire(oc("session.idle"), env, rec, now=1_000_000_090_000)
    (entry,) = entries(env)
    assert entry["status"] == "done" and entry["ended_ms"] == 1_000_000_090_000
    assert rec.calls == [(PANE, "1/1"), (PANE, "0/1")]


def test_status_idle_finishes_and_error_fails(env):
    rec = Recorder()
    fire(oc("session.created", child="a"), env, rec)
    fire(oc("session.created", child="b"), env, rec)
    fire(oc("session.status", child="a", status="idle"), env, rec)
    fire(oc("session.error", child="b"), env, rec)
    assert {e["id"]: e["status"] for e in entries(env)} == {"a": "done", "b": "failed"}


def test_deleted_child_counts_as_finished(env):
    rec = Recorder()
    fire(oc("session.created"), env, rec)
    fire(oc("session.deleted"), env, rec)
    assert entries(env)[0]["status"] == "done"


def test_a_missed_created_is_recovered_by_a_heartbeat(env):
    fire(oc("session.status", status="busy", title="plain title"), env, Recorder())
    (entry,) = entries(env)
    assert entry["status"] == "running" and entry["description"] == "plain title"
    assert entry["type"] == ""


@pytest.mark.parametrize(
    "payload",
    [
        oc("session.created", parent=""),  # a root session: never a subagent
        oc("session.created", child=""),
        oc("session.status", status="mystery"),
        oc("message.updated"),
        {"hook_event_name": "SubagentStart", "agent_id": "x"},  # Claude shape
    ],
)
def test_irrelevant_opencode_payloads_are_ignored(payload, env):
    rec = Recorder()
    fire(payload, env, rec)
    assert rec.calls == []
    assert not os.path.exists(hook.spool_path(PANE, env))


def test_a_new_root_session_resets_the_spool(env):
    rec = Recorder()
    fire(oc("session.created", child="a", root="ses_one", parent="ses_one"), env, rec)
    fire(oc("session.created", child="b", root="ses_two", parent="ses_two"), env, rec)
    assert [e["id"] for e in entries(env)] == ["b"]


# --- the plugin under node ---------------------------------------------------------------

NODE = shutil.which("node")


def _capture_hook(tmp_path: Path) -> tuple[Path, Path]:
    """An executable standing in for herdeck-subagent-hook: appends its argv
    and stdin as one JSON line."""
    out = tmp_path / "calls.jsonl"
    script = tmp_path / "fake-hook"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json, sys
            with open({str(out)!r}, "a") as fh:
                fh.write(json.dumps({{"argv": sys.argv[1:], "stdin": sys.stdin.read()}}) + "\\n")
            """
        )
    )
    script.chmod(0o755)
    return script, out


def _run_plugin(tmp_path: Path, events: list[dict], env_extra: dict) -> list[dict]:
    script, out = _capture_hook(tmp_path)
    plugin = tmp_path / "herdeck-subagents.mjs"
    plugin.write_text(hi.opencode_plugin_source(str(script)))
    driver = tmp_path / "driver.mjs"
    driver.write_text(
        textwrap.dedent(
            f"""\
            import mod from {json.dumps(plugin.as_uri())};
            const hooks = await mod.server();
            for (const event of {json.dumps(events)}) {{
              if (hooks.event) await hooks.event({{ event }});
            }}
            """
        )
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
    env.update(env_extra)
    subprocess.run([NODE, str(driver)], check=True, env=env, timeout=30)
    return _settled_lines(out)


def _settled_lines(path: Path) -> list[dict]:
    """The captured calls once no new one arrived for 0.5 s (the hook is
    spawned without being awaited, so it may still be running)."""
    import time

    deadline = time.monotonic() + 10
    last, quiet_since = -1, time.monotonic()
    while time.monotonic() < deadline:
        size = path.stat().st_size if path.exists() else 0
        if size != last:
            last, quiet_since = size, time.monotonic()
        elif time.monotonic() - quiet_since >= 0.5:
            break
        time.sleep(0.05)
    lines = path.read_text().splitlines() if path.exists() else []
    return [json.loads(line) for line in lines]


def _session(id_, parent=None, title="t"):
    info = {"id": id_, "title": title}
    if parent:
        info["parentID"] = parent
    return {"type": "session.created", "properties": {"info": info}}


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_plugin_reports_only_child_sessions(tmp_path):
    events = [
        _session("root"),
        _session("kid", parent="root", title="Scan the repo (@explore subagent)"),
        _session("grandkid", parent="kid"),
        {"type": "session.status", "properties": {"sessionID": "root", "status": {"type": "busy"}}},
        {"type": "session.status", "properties": {"sessionID": "kid", "status": {"type": "busy"}}},
        {"type": "session.status", "properties": {"sessionID": "kid", "status": {"type": "busy"}}},
        {"type": "session.status", "properties": {"sessionID": "kid", "status": {"type": "idle"}}},
        {"type": "session.idle", "properties": {"sessionID": "kid"}},
        {"type": "session.error", "properties": {"sessionID": "grandkid"}},
        {"type": "session.idle", "properties": {"sessionID": "root"}},
    ]
    calls = _run_plugin(tmp_path, events, {"HERDR_PANE_ID": PANE})
    assert all(c["argv"] == ["--provider", "opencode"] for c in calls)
    payloads = sorted(
        (json.loads(c["stdin"]) for c in calls), key=lambda p: (p["session_id"], p["hook_event_name"])
    )
    summary = [(p["hook_event_name"], p["session_id"]) for p in payloads]
    # one heartbeat (throttled), one idle (status idle + session.idle deduped)
    assert sorted(summary) == sorted(
        [
            ("session.created", "kid"),
            ("session.status", "kid"),
            ("session.idle", "kid"),
            ("session.created", "grandkid"),
            ("session.error", "grandkid"),
        ]
    )
    grandkid = next(p for p in payloads if p["session_id"] == "grandkid")
    assert grandkid["root_session_id"] == "root" and grandkid["depth"] == 2
    assert grandkid["parent_id"] == "kid"
    kid = next(p for p in payloads if p["hook_event_name"] == "session.created" and p["session_id"] == "kid")
    assert kid["title"] == "Scan the repo (@explore subagent)"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_plugin_is_inert_outside_a_herdr_pane(tmp_path):
    calls = _run_plugin(tmp_path, [_session("kid", parent="root")], {})
    assert calls == []


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_plugin_survives_a_missing_hook(tmp_path):
    plugin = tmp_path / "p.mjs"
    plugin.write_text(hi.opencode_plugin_source(str(tmp_path / "does-not-exist")))
    driver = tmp_path / "d.mjs"
    driver.write_text(
        f"import mod from {json.dumps(plugin.as_uri())};\n"
        "const h = await mod.server();\n"
        "await h.event({ event: { type: 'session.created', properties: { info: { id: 'k', parentID: 'r' } } } });\n"
        "console.log('alive');\n"
    )
    env = dict(os.environ, HERDR_PANE_ID=PANE)
    result = subprocess.run([NODE, str(driver)], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and "alive" in result.stdout


# --- install matrix ---------------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "OPENCODE_CONFIG_DIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    return home


def _plugins(home: Path) -> Path:
    return home / ".config" / "opencode" / "plugins"


def _backups(directory: Path) -> list[Path]:
    return sorted(p for p in directory.iterdir() if ".bak-herdeck-" in p.name)


def test_install_writes_the_plugin_with_the_hook_path(home):
    result = hi.apply("install", ["opencode"], home=home, hook_path=HOOK)
    assert result["ok"] is True
    r = result["agents"]["opencode"]
    path = _plugins(home) / "herdeck-subagents.js"
    assert r["file"] == str(path) and r["installed"] is True and r["changed"] is True
    assert r["backup"] is None and r["command"] == HOOK
    text = path.read_text()
    assert f"const HOOK = {json.dumps(HOOK)};" in text and hi.OPENCODE_MARKER in text


def test_install_is_idempotent_and_leaves_other_plugins_alone(home):
    plugins = _plugins(home)
    plugins.mkdir(parents=True)
    (plugins / "herdr-agent-state.js").write_text(HERDR_PLUGIN)
    hi.apply("install", ["opencode"], home=home, hook_path=HOOK)
    again = hi.apply("install", ["opencode"], home=home, hook_path=HOOK)
    assert again["agents"]["opencode"]["changed"] is False
    assert _backups(plugins) == []
    assert (plugins / "herdr-agent-state.js").read_text() == HERDR_PLUGIN
    hi.apply("uninstall", ["opencode"], home=home)
    assert sorted(p.name for p in plugins.iterdir() if ".bak-" not in p.name) == ["herdr-agent-state.js"]
    assert (plugins / "herdr-agent-state.js").read_text() == HERDR_PLUGIN


def test_reinstall_with_a_new_hook_path_backs_up_the_old_plugin(home):
    hi.apply("install", ["opencode"], home=home, hook_path=HOOK)
    old = (_plugins(home) / "herdeck-subagents.js").read_text()
    result = hi.apply("install", ["opencode"], home=home, hook_path="/new/herdeck-subagent-hook")
    r = result["agents"]["opencode"]
    assert r["changed"] is True and r["command"] == "/new/herdeck-subagent-hook"
    (backup,) = _backups(_plugins(home))
    assert backup.read_text() == old and str(backup) == r["backup"]
    # a backup never ends in .js: OpenCode must not load it as a plugin
    assert not backup.name.endswith((".js", ".ts"))


def test_a_foreign_file_of_our_name_is_never_replaced_or_removed(home):
    plugins = _plugins(home)
    plugins.mkdir(parents=True)
    foreign = plugins / "herdeck-subagents.js"
    foreign.write_text("// my own plugin\n")
    for action in ("install", "uninstall"):
        result = hi.apply(action, ["opencode"], home=home, hook_path=HOOK)
        assert result["ok"] is False
        assert "not herdeck's plugin" in result["agents"]["opencode"]["error"]
        assert foreign.read_text() == "// my own plugin\n"
    status = hi.apply("status", ["opencode"], home=home)["agents"]["opencode"]
    assert status["installed"] is False and status["error"]


def test_uninstall_removes_the_plugin_after_a_backup(home):
    hi.apply("install", ["opencode"], home=home, hook_path=HOOK)
    result = hi.apply("uninstall", ["opencode"], home=home)
    r = result["agents"]["opencode"]
    assert r["changed"] is True and r["installed"] is False and r["backup"]
    assert not (_plugins(home) / "herdeck-subagents.js").exists()
    assert len(_backups(_plugins(home))) == 1


def test_uninstall_of_nothing_changes_nothing(home):
    result = hi.apply("uninstall", ["opencode"], home=home)
    assert result["ok"] is True and result["agents"]["opencode"]["changed"] is False
    assert not (home / ".config").exists()


def test_opencode_config_dir_env_wins(home, tmp_path):
    custom = tmp_path / "oc-config"
    env = {"OPENCODE_CONFIG_DIR": str(custom), "XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    result = hi.apply("install", ["opencode"], home=home, hook_path=HOOK, env=env)
    r = result["agents"]["opencode"]
    assert r["file"] == str(custom / "plugins" / "herdeck-subagents.js")
    assert r["config_dir_source"] == "OPENCODE_CONFIG_DIR"
    assert (custom / "plugins" / "herdeck-subagents.js").exists()


def test_xdg_config_home_is_honoured(home, tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    r = hi.apply("status", ["opencode"], home=home, env=env)["agents"]["opencode"]
    assert r["file"] == str(tmp_path / "xdg" / "opencode" / "plugins" / "herdeck-subagents.js")
    assert r["config_dir_source"] == "XDG_CONFIG_HOME"


def test_default_install_includes_opencode_only_where_it_is_set_up(home):
    result = hi.apply("install", home=home, hook_path=HOOK)
    assert set(result["agents"]) == {"claude", "codex"}
    assert not (home / ".config").exists()
    (home / ".config" / "opencode").mkdir(parents=True)
    result = hi.apply("install", home=home, hook_path=HOOK)
    assert set(result["agents"]) == {"claude", "codex", "opencode"}
    assert result["agents"]["opencode"]["installed"] is True
    # status and uninstall always cover every agent
    assert set(hi.apply("status", home=home)["agents"]) == set(hi.AGENTS)


def test_a_symlinked_plugin_keeps_its_link(home, tmp_path):
    hi.apply("install", ["opencode"], home=home, hook_path=HOOK)
    target = tmp_path / "dotfiles" / "herdeck-subagents.js"
    target.parent.mkdir()
    link = _plugins(home) / "herdeck-subagents.js"
    target.write_text(link.read_text())
    link.unlink()
    link.symlink_to(target)
    hi.apply("install", ["opencode"], home=home, hook_path="/new/herdeck-subagent-hook")
    assert link.is_symlink()
    assert "/new/herdeck-subagent-hook" in target.read_text()


def test_summary_and_cli_describe_opencode(home, capsys):
    from herdeck import service

    result = hi.apply("install", ["opencode"], home=home, hook_path=HOOK)
    assert hi.summary(result["agents"])["opencode"] == {
        "installed": True,
        "file": str(_plugins(home) / "herdeck-subagents.js"),
        "error": None,
    }
    with pytest.raises(SystemExit) as e:
        service.main(["hooks", "status", "--agents", "opencode"])
    assert e.value.code == 0
    assert "OpenCode: installed" in capsys.readouterr().out


async def test_bridge_message_installs_opencode(home):
    reply = await hi.bridge_reply(
        {"type": "hooks", "req": "h1", "action": "install", "agents": ["opencode"]}, hook_path=HOOK
    )
    assert reply["type"] == "result"
    assert reply["data"]["agents"]["opencode"]["installed"] is True


def test_the_shipped_plugin_is_package_data():
    import tomllib

    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text())
    patterns = data["tool"]["setuptools"]["package-data"]["herdeck"]
    assert "assets/opencode/*" in patterns
    assert (root / "src" / "herdeck" / "assets" / "opencode" / "herdeck-subagents.js").is_file()
