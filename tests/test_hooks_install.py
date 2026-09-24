"""herdeck-service hooks install|uninstall|status (hooks_install.py).

Every test works in a temp HOME: the real ~/.claude and ~/.codex are never
read or written."""

import asyncio
import copy
import json
import os
import stat
from pathlib import Path

import pytest

from herdeck import hooks_install as hi
from herdeck import service

HOOK = "/opt/herdeck/venv/bin/herdeck-subagent-hook"
CLAUDE_CMD = f"{HOOK} --provider claude"
CODEX_CMD = f"{HOOK} --provider codex"

HERDR_CLAUDE = {"type": "command", "command": "/Users/x/.local/bin/herdr-hook claude"}
MOSHI = {"type": "command", "command": "moshi-notify --stop", "timeout": 3}

# A realistic Claude settings.json: other keys, other agents' hooks on the same
# events (herdr, moshi), an event we never touch, unusual formatting.
CLAUDE_SETTINGS = {
    "$schema": "https://json.schemastore.org/claude-code-settings.json",
    "model": "opus",
    "permissions": {"allow": ["Bash(git status)"], "deny": []},
    "env": {"FOO": "bär"},
    "hooks": {
        "SessionStart": [{"hooks": [HERDR_CLAUDE]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [HERDR_CLAUDE, MOSHI]}],
        "Stop": [{"hooks": [MOSHI]}],
    },
    "statusLine": {"type": "command", "command": "~/.claude/statusline.sh"},
}

CODEX_HOOKS = {
    "hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": "herdr-hook codex"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "herdwatch notify"}]}],
    }
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert Path.home() == home
    return home


def _write(path: Path, data, raw=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw if raw is not None else json.dumps(data, indent=4))


def _read(path: Path):
    return json.loads(path.read_text())


def _claude(home):
    return home / ".claude" / "settings.json"


def _codex(home):
    return home / ".codex" / "hooks.json"


def _ours(doc, event):
    return [
        h
        for g in doc["hooks"].get(event, [])
        for h in g.get("hooks", [])
        if "herdeck-subagent-hook" in h.get("command", "")
    ]


def _backups(path: Path):
    return sorted(path.parent.glob(path.name + ".bak-herdeck-*"))


# --- install ---------------------------------------------------------------------


def test_install_claude_appends_ours_and_keeps_everything_else(home):
    _write(_claude(home), CLAUDE_SETTINGS)
    result = hi.apply("install", ["claude"], home=home, hook_path=HOOK)

    assert result["ok"] is True
    r = result["agents"]["claude"]
    assert r["installed"] is True and r["changed"] is True
    doc = _read(_claude(home))
    # other keys untouched
    for key in ("$schema", "model", "permissions", "env", "statusLine"):
        assert doc[key] == CLAUDE_SETTINGS[key]
    # other entries untouched and still first; ours appended
    assert doc["hooks"]["SessionStart"][0] == {"hooks": [HERDR_CLAUDE]}
    assert doc["hooks"]["SessionStart"][1] == {
        "matcher": "startup|clear",
        "hooks": [{"type": "command", "command": CLAUDE_CMD, "timeout": 5, "async": True}],
    }
    assert doc["hooks"]["PreToolUse"][0] == {"matcher": "Bash", "hooks": [HERDR_CLAUDE, MOSHI]}
    assert doc["hooks"]["PreToolUse"][1]["matcher"] == "*"
    assert doc["hooks"]["PostToolUse"] == [
        {
            "matcher": "Agent|Task",
            "hooks": [{"type": "command", "command": CLAUDE_CMD, "timeout": 5, "async": True}],
        }
    ]
    assert "matcher" not in doc["hooks"]["SubagentStart"][0]
    assert doc["hooks"]["Stop"] == [{"hooks": [MOSHI]}]
    assert _read(_claude(home))["env"]["FOO"] == "bär"
    assert "bär" in _claude(home).read_text()  # no \\u escapes
    # backup holds the original bytes
    (backup,) = _backups(_claude(home))
    assert r["backup"] == str(backup)
    assert json.loads(backup.read_text()) == CLAUDE_SETTINGS


def test_install_codex_writes_the_documented_entries(home):
    _write(_codex(home), CODEX_HOOKS)
    r = hi.apply("install", ["codex"], home=home, hook_path=HOOK)["agents"]["codex"]
    doc = _read(_codex(home))
    assert r["installed"] is True
    assert r["needs_trust"] is True
    assert r["features_hooks_enabled"] is False  # no config.toml
    for event in ("SubagentStart", "SubagentStop", "SessionStart"):
        assert _ours(doc, event) == [{"type": "command", "command": CODEX_CMD, "timeout": 5}]
    assert doc["hooks"]["SessionStart"][0] == CODEX_HOOKS["hooks"]["SessionStart"][0]
    assert doc["hooks"]["Stop"] == CODEX_HOOKS["hooks"]["Stop"]
    assert "PreToolUse" not in doc["hooks"]
    # never switched on for the user
    assert not (home / ".codex" / "config.toml").exists()


def test_install_is_idempotent(home):
    _write(_claude(home), CLAUDE_SETTINGS)
    _write(_codex(home), CODEX_HOOKS)
    hi.apply("install", home=home, hook_path=HOOK)
    first = {p: p.read_bytes() for p in (_claude(home), _codex(home))}
    backups = _backups(_claude(home)) + _backups(_codex(home))

    again = hi.apply("install", home=home, hook_path=HOOK)

    assert all(r["changed"] is False and r["backup"] is None for r in again["agents"].values())
    assert {p: p.read_bytes() for p in first} == first
    assert _backups(_claude(home)) + _backups(_codex(home)) == backups
    doc = _read(_claude(home))
    assert len(_ours(doc, "PreToolUse")) == 1


def test_install_creates_a_missing_file_without_a_backup(home):
    result = hi.apply("install", home=home, hook_path=HOOK)
    for agent, path in (("claude", _claude(home)), ("codex", _codex(home))):
        r = result["agents"][agent]
        assert r["installed"] and r["changed"] and r["backup"] is None
        assert path.exists() and _backups(path) == []
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert set(_read(_claude(home))["hooks"]) == {e for e, _ in hi.EVENTS["claude"]}


def test_install_keeps_the_file_mode(home):
    _write(_claude(home), CLAUDE_SETTINGS)
    os.chmod(_claude(home), 0o644)
    hi.apply("install", ["claude"], home=home, hook_path=HOOK)
    assert stat.S_IMODE(_claude(home).stat().st_mode) == 0o644


def test_install_replaces_an_outdated_entry_of_ours(home):
    doc = copy.deepcopy(CLAUDE_SETTINGS)
    old = {"type": "command", "command": "/old/bin/herdeck-subagent-hook --provider claude"}
    doc["hooks"]["PreToolUse"].append({"matcher": "*", "hooks": [old]})
    doc["hooks"]["PreToolUse"][0]["hooks"].append(old)  # also mixed into another group
    _write(_claude(home), doc)

    hi.apply("install", ["claude"], home=home, hook_path=HOOK)

    out = _read(_claude(home))
    assert _ours(out, "PreToolUse") == [
        {"type": "command", "command": CLAUDE_CMD, "timeout": 5, "async": True}
    ]
    assert out["hooks"]["PreToolUse"][0] == {"matcher": "Bash", "hooks": [HERDR_CLAUDE, MOSHI]}


def test_install_quotes_a_hook_path_with_spaces(home):
    hi.apply("install", ["claude"], home=home, hook_path="/Users/a b/bin/herdeck-subagent-hook")
    (hook,) = _ours(_read(_claude(home)), "SubagentStart")
    assert hook["command"] == "'/Users/a b/bin/herdeck-subagent-hook' --provider claude"


def test_install_without_a_hook_executable_fails_cleanly(home, monkeypatch):
    monkeypatch.setattr(hi, "resolve_hook_path", lambda explicit=None: None)
    result = hi.apply("install", ["claude"], home=home)
    assert result["ok"] is False
    assert "not found" in result["agents"]["claude"]["error"]
    assert not _claude(home).exists()


def test_resolve_hook_path_prefers_arg_then_sibling_then_path(tmp_path, monkeypatch):
    assert hi.resolve_hook_path("/x/hook") == "/x/hook"
    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    monkeypatch.setattr(hi.sys, "executable", str(bindir / "python"))
    monkeypatch.setattr(hi.shutil, "which", lambda name: "/usr/local/bin/" + name)
    assert hi.resolve_hook_path() == "/usr/local/bin/herdeck-subagent-hook"
    sibling = bindir / "herdeck-subagent-hook"
    sibling.write_text("#!/bin/sh\n")
    sibling.chmod(0o755)
    assert hi.resolve_hook_path() == str(sibling)


def test_env_overrides_pick_the_agent_config_dirs(tmp_path):
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "cc"), "CODEX_HOME": str(tmp_path / "cx")}
    assert hi.hook_file("claude", tmp_path, env) == tmp_path / "cc" / "settings.json"
    assert hi.hook_file("codex", tmp_path, env) == tmp_path / "cx" / "hooks.json"


# --- refusal -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        '{"hooks": {"SubagentStart": [}',  # broken JSON
        "[1, 2]",  # not an object
        '{"hooks": []}',  # hooks not an object
        '{"hooks": {"Stop": {"hooks": []}}}',  # event not an array
        "// comment\n{}",  # JSONC is not JSON
    ],
)
@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_an_unusable_file_is_reported_and_never_overwritten(home, raw, action):
    _write(_claude(home), None, raw=raw)
    result = hi.apply(action, ["claude"], home=home, hook_path=HOOK)
    r = result["agents"]["claude"]
    assert result["ok"] is False and r["error"] and "not changed" in r["error"]
    assert r["changed"] is False and r["installed"] is False
    assert _claude(home).read_text() == raw
    assert _backups(_claude(home)) == []
    assert hi.agent_status("claude", home)["error"]


def test_a_file_changed_during_the_edit_is_not_clobbered(home, monkeypatch):
    _write(_claude(home), CLAUDE_SETTINGS)
    real_load = hi._load

    def racing_load(path):
        out = real_load(path)
        path.write_text('{"changed": "by the agent"}')
        return out

    monkeypatch.setattr(hi, "_load", racing_load)
    r = hi.apply("install", ["claude"], home=home, hook_path=HOOK)["agents"]["claude"]
    assert "changed while" in r["error"]
    assert _read(_claude(home)) == {"changed": "by the agent"}


# --- uninstall ---------------------------------------------------------------------


def test_uninstall_restores_the_other_entries(home):
    _write(_claude(home), CLAUDE_SETTINGS)
    _write(_codex(home), CODEX_HOOKS)
    hi.apply("install", home=home, hook_path=HOOK)

    result = hi.apply("uninstall", home=home)

    assert result["ok"] is True
    assert _read(_claude(home)) == CLAUDE_SETTINGS
    assert _read(_codex(home)) == CODEX_HOOKS
    for r in result["agents"].values():
        assert r["installed"] is False and r["events"] == [] and r["changed"] is True
        assert r["backup"]
    assert result["agents"]["codex"]["needs_trust"] is False


def test_uninstall_removes_ours_from_mixed_groups_only(home):
    doc = copy.deepcopy(CLAUDE_SETTINGS)
    ours = {"type": "command", "command": CLAUDE_CMD}
    doc["hooks"]["PreToolUse"][0]["hooks"].append(ours)
    _write(_claude(home), doc)
    hi.apply("uninstall", ["claude"], home=home)
    assert _read(_claude(home)) == CLAUDE_SETTINGS


def test_uninstall_drops_an_emptied_hooks_object(home):
    hi.apply("install", ["claude"], home=home, hook_path=HOOK)
    hi.apply("uninstall", ["claude"], home=home)
    assert _read(_claude(home)) == {}


def test_uninstall_of_nothing_writes_nothing(home):
    _write(_claude(home), CLAUDE_SETTINGS)
    before = _claude(home).read_bytes()
    result = hi.apply("uninstall", home=home)
    assert all(r["changed"] is False for r in result["agents"].values())
    assert _claude(home).read_bytes() == before
    assert not _codex(home).exists()
    assert _backups(_claude(home)) == []


# --- status ------------------------------------------------------------------------


def test_status_reports_partial_installs(home):
    doc = {"hooks": {"SubagentStart": [{"hooks": [{"type": "command", "command": CLAUDE_CMD}]}]}}
    _write(_claude(home), doc)
    r = hi.agent_status("claude", home)
    assert r["installed"] is False
    assert r["events"] == ["SubagentStart"]
    assert "PreToolUse" in r["missing_events"]
    assert r["command"] == CLAUDE_CMD


@pytest.mark.parametrize(
    "toml, expected",
    [
        (None, False),
        ("model = 'o3'\n", False),
        ("[features]\nhooks = false\n", False),
        ("[features]\nhooks = true\n", True),
        ("[features]\nhooks = 'yes'\n", False),
        ("[features\nbroken", None),
    ],
)
def test_codex_features_flag_detection(home, toml, expected):
    if toml is not None:
        (home / ".codex").mkdir()
        (home / ".codex" / "config.toml").write_text(toml)
    assert hi.agent_status("codex", home)["features_hooks_enabled"] is expected
    if toml is not None:
        assert (home / ".codex" / "config.toml").read_text() == toml


def test_summary_is_the_compact_maintenance_view(home):
    result = hi.apply("install", home=home, hook_path=HOOK)
    assert hi.summary(result["agents"]) == {
        "claude": {"installed": True, "file": str(_claude(home)), "error": None},
        "codex": {
            "installed": True,
            "file": str(_codex(home)),
            "error": None,
            "needs_trust": True,
            "features_hooks_enabled": False,
        },
    }


def test_apply_rejects_unknown_actions_and_agents(home):
    with pytest.raises(ValueError):
        hi.apply("enable", home=home)
    with pytest.raises(ValueError):
        hi.apply("status", ["opencode"], home=home)


# --- bridge message ------------------------------------------------------------------


async def test_bridge_reply_runs_the_action(home):
    reply = await hi.bridge_reply(
        {"type": "hooks", "req": "h1", "action": "install", "agents": ["codex"]}, hook_path=HOOK
    )
    assert reply["type"] == "result" and reply["req"] == "h1"
    assert reply["data"]["agents"]["codex"]["installed"] is True
    assert set(reply["data"]["agents"]) == {"codex"}
    status = await hi.bridge_reply({"type": "hooks", "req": "h2"})
    assert set(status["data"]["agents"]) == {"claude", "codex"}
    assert status["data"]["action"] == "status"


@pytest.mark.parametrize(
    "msg",
    [
        {"action": "enable"},
        {"action": "install", "agents": ["opencode"]},
        {"action": "install", "agents": []},
        {"action": "install", "agents": 5},
        {"action": "install", "agents": "claude,pi"},
    ],
)
async def test_bridge_reply_refuses_bad_requests(home, msg):
    msg = {"type": "hooks", "req": "h", **msg}
    reply = await hi.bridge_reply(msg)
    assert reply == {"type": "error", "req": "h", "message": "hooks: invalid action or agents"}
    assert not _claude(home).exists()


async def test_bridge_reply_times_out(home, monkeypatch):
    import time as _time

    monkeypatch.setattr(hi, "apply", lambda *a, **k: _time.sleep(0.5))
    reply = await hi.bridge_reply({"type": "hooks", "req": "h"}, timeout=0.05)
    assert reply["type"] == "error" and "timed out" in reply["message"]
    await asyncio.sleep(0.5)


# --- CLI -----------------------------------------------------------------------------


def test_service_cli_dispatches_hooks(home, capsys):
    with pytest.raises(SystemExit) as e:
        service.main(["hooks", "install", "--agents", "claude", "--hook-path", HOOK, "--json"])
    assert e.value.code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["agents"]["claude"]["installed"] is True
    with pytest.raises(SystemExit) as e:
        service.main(["hooks", "status"])
    text = capsys.readouterr().out
    assert e.value.code == 0
    assert "Claude Code: installed" in text and "Codex: not installed" in text


def test_service_cli_exit_code_reports_errors(home, capsys):
    _write(_codex(home), None, raw="not json")
    with pytest.raises(SystemExit) as e:
        service.main(["hooks", "install", "--hook-path", HOOK])
    assert e.value.code == 1
    text = capsys.readouterr().out
    assert "Codex: error:" in text and "Claude Code: installed" in text


def test_service_cli_rejects_unknown_agents(home):
    with pytest.raises(SystemExit) as e:
        service.main(["hooks", "status", "--agents", "claude,pi"])
    assert e.value.code == 2
