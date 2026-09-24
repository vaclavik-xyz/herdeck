"""Install / remove / inspect the ``herdeck-subagent-hook`` agent hooks.

``herdeck-service hooks install|uninstall|status [--agents claude,codex]``
edits the hook files README "Subagent tracking" documents:

* Claude Code: ``~/.claude/settings.json`` (``$CLAUDE_CONFIG_DIR/settings.json``
  when set), events ``SubagentStart``, ``SubagentStop``, ``PostToolUse``
  (matcher ``Agent|Task``), ``PreToolUse`` (matcher ``*``) and ``SessionStart``
  (matcher ``startup|clear``); each entry is async with a 5 s timeout.
* Codex: ``~/.codex/hooks.json`` (``$CODEX_HOME/hooks.json`` when set), events
  ``SubagentStart``, ``SubagentStop``, ``SessionStart``. Codex only runs hooks
  with ``[features] hooks = true`` in ``config.toml`` — reported as
  ``features_hooks_enabled`` and NEVER switched on here — and asks the user to
  trust new hooks (``/hooks`` in a session). Codex keeps that trust record
  itself, so ``needs_trust`` is simply true whenever our Codex hooks are
  installed: the UI reminds, it cannot verify.

Our entries are recognised by their command's program (first shell word)
being named ``herdeck-subagent-hook``; every other hook (herdr, herdwatch, moshi, the
user's own) and every other key stays as it was. A write goes through a JSON
round-trip (``indent=2``), is preceded by a byte copy of the old file to
``<file>.bak-herdeck-<timestamp>`` and lands atomically (temp file + rename,
the file's mode kept). A file that is not valid JSON (or whose ``hooks`` is
not an object of arrays) is reported and never overwritten. Installing twice
changes nothing the second time.

The bridge runs this in-process for the ``hooks`` message (full token only),
so the desktop Maintenance section can switch tracking on for the agents' Mac.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import shutil
import sys
import tempfile
import threading
import time
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path

HOOK_NAME = "herdeck-subagent-hook"
AGENTS = ("claude", "codex")
ACTIONS = ("status", "install", "uninstall")
CAPABILITY = "hooks"
HOOK_TIMEOUT_S = 5
# The bridge caps one hooks request (file IO only) at this.
BRIDGE_TIMEOUT_S = 10.0

# (event, matcher) per agent, in the order README documents them.
EVENTS: dict[str, tuple[tuple[str, str | None], ...]] = {
    "claude": (
        ("SubagentStart", None),
        ("SubagentStop", None),
        ("PostToolUse", "Agent|Task"),
        ("PreToolUse", "*"),
        ("SessionStart", "startup|clear"),
    ),
    "codex": (
        ("SubagentStart", None),
        ("SubagentStop", None),
        ("SessionStart", None),
    ),
}

# One install/uninstall at a time per process (the bridge serves many clients).
_WRITE_LOCK = threading.Lock()


class HookFileError(Exception):
    """The hook file cannot be used safely (unreadable, not JSON, odd shape)."""


# The env var each agent reads for its config directory. The bridge usually
# runs under launchd/systemd without the user's shell environment: a user who
# relies on one of these must set it in the bridge service's env too
# (``herdeck-service install bridge --env CLAUDE_CONFIG_DIR=...``); status
# reports which directory was used and where it came from.
CONFIG_DIR_ENV = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}


def config_dir(agent: str, home: Path, env: Mapping[str, str] | None = None) -> tuple[Path, str]:
    """(the agent's config directory, its source: the env var name or "home")."""
    env = os.environ if env is None else env
    base = env.get(CONFIG_DIR_ENV[agent])
    if base:
        return Path(base), CONFIG_DIR_ENV[agent]
    return home / (".claude" if agent == "claude" else ".codex"), "home"


def hook_file(agent: str, home: Path, env: Mapping[str, str] | None = None) -> Path:
    directory, _ = config_dir(agent, home, env)
    return directory / ("settings.json" if agent == "claude" else "hooks.json")


def codex_config_file(home: Path, env: Mapping[str, str] | None = None) -> Path:
    return hook_file("codex", home, env).with_name("config.toml")


def resolve_hook_path(explicit: str | os.PathLike | None = None) -> str | None:
    """The hook executable: ``explicit``, else next to this interpreter (a
    venv's ``bin/``), else on ``PATH``. None when none is found."""
    if explicit:
        return str(explicit)
    sibling = Path(sys.executable).parent / HOOK_NAME
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    return shutil.which(HOOK_NAME)


def hook_command(hook_path: str, agent: str) -> str:
    return f"{shlex.quote(hook_path)} --provider {agent}"


def _is_ours(hook: object) -> bool:
    """Our entry: the command's program (first shell word) is named
    ``herdeck-subagent-hook`` — never a command that merely mentions it."""
    if not isinstance(hook, dict) or not isinstance(hook.get("command"), str):
        return False
    try:
        words = shlex.split(hook["command"])
    except ValueError:
        return False
    return bool(words) and os.path.basename(words[0]) == HOOK_NAME


def _entry(agent: str, command: str, matcher: str | None) -> dict:
    hook: dict = {"type": "command", "command": command, "timeout": HOOK_TIMEOUT_S}
    if agent == "claude":
        hook["async"] = True
    group: dict = {}
    if matcher is not None:
        group["matcher"] = matcher
    group["hooks"] = [hook]
    return group


# --- reading ------------------------------------------------------------------


def _load(path: Path) -> tuple[dict | None, bytes | None]:
    """(document, raw bytes); (None, None) when the file does not exist."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise HookFileError(f"cannot read {path}: {exc.strerror or exc}") from exc
    try:
        doc = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (UnicodeDecodeError, ValueError) as exc:
        raise HookFileError(f"{path} is not valid JSON ({exc}); not changed") from exc
    if not isinstance(doc, dict):
        raise HookFileError(f"{path} does not hold a JSON object; not changed")
    hooks = doc.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise HookFileError(f'"hooks" in {path} is not an object; not changed')
    for event, groups in (hooks or {}).items():
        if not isinstance(groups, list):
            raise HookFileError(f'"hooks.{event}" in {path} is not an array; not changed')
        for group in groups:
            if isinstance(group, dict) and not isinstance(group.get("hooks", []), list):
                raise HookFileError(
                    f'a "hooks.{event}" entry in {path} has "hooks" that is not an array; '
                    "not changed"
                )
    return doc, raw


def _our_events(doc: dict | None) -> list[str]:
    hooks = (doc or {}).get("hooks") or {}
    found = []
    for event, groups in hooks.items():
        for group in groups:
            if isinstance(group, dict) and any(_is_ours(h) for h in group.get("hooks") or []):
                found.append(event)
                break
    return found


def _our_command(doc: dict | None) -> str | None:
    for groups in ((doc or {}).get("hooks") or {}).values():
        for group in groups:
            if not isinstance(group, dict):
                continue
            for hook in group.get("hooks") or []:
                if _is_ours(hook):
                    return str(hook.get("command"))
    return None


def codex_features_hooks(home: Path, env: Mapping[str, str] | None = None) -> bool | None:
    """``[features] hooks`` in Codex's config.toml: True/False, None when the
    file cannot be parsed (a missing file means the default, off)."""
    path = codex_config_file(home, env)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    features = data.get("features")
    return isinstance(features, dict) and features.get("hooks") is True


def agent_status(agent: str, home: Path, env: Mapping[str, str] | None = None) -> dict:
    path = hook_file(agent, home, env)
    directory, source = config_dir(agent, home, env)
    wanted = [event for event, _ in EVENTS[agent]]
    out: dict = {
        "agent": agent,
        "file": str(path),
        "config_dir": str(directory),
        "config_dir_source": source,
        "file_exists": path.exists(),
        "installed": False,
        "events": [],
        "missing_events": wanted,
        "command": None,
        "error": None,
    }
    try:
        doc, _ = _load(path)
    except HookFileError as exc:
        out["error"] = str(exc)
    else:
        present = _our_events(doc)
        out["events"] = [e for e in wanted if e in present]
        out["missing_events"] = [e for e in wanted if e not in present]
        out["installed"] = not out["missing_events"]
        out["command"] = _our_command(doc)
    if agent == "codex":
        out["features_hooks_enabled"] = codex_features_hooks(home, env)
        out["needs_trust"] = bool(out["events"])
    return out


# --- editing ------------------------------------------------------------------


def _strip_ours(groups: list) -> list:
    kept = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept.append(group)
            continue
        hooks = group["hooks"]
        remaining = [h for h in hooks if not _is_ours(h)]
        if len(remaining) == len(hooks):
            kept.append(group)
        elif remaining:
            kept.append({**group, "hooks": remaining})
        # else: the group held only our hooks -> dropped
    return kept


def _installed(doc: dict, agent: str, command: str) -> dict:
    hooks = dict(doc.get("hooks") or {})
    for event, matcher in EVENTS[agent]:
        groups = list(hooks.get(event) or [])
        desired = _entry(agent, command, matcher)
        ours = [g for g in groups if isinstance(g, dict) and any(_is_ours(h) for h in g.get("hooks") or [])]
        if ours == [desired]:
            continue  # exactly our entry already: keep its position
        hooks[event] = [*_strip_ours(groups), desired]
    return {**doc, "hooks": hooks}


def _uninstalled(doc: dict) -> dict:
    if "hooks" not in doc:
        return doc
    hooks = {}
    for event, groups in doc["hooks"].items():
        kept = _strip_ours(groups)
        if kept or not groups:
            hooks[event] = kept
        # else: the event only held our entries -> the key goes too
    out = dict(doc)
    if hooks or not doc["hooks"]:
        out["hooks"] = hooks
    else:
        del out["hooks"]
    return out


def _backup_path(path: Path, now: float | None = None) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    candidate = path.with_name(f"{path.name}.bak-herdeck-{stamp}")
    n = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak-herdeck-{stamp}-{n}")
        n += 1
    return candidate


def _write(path: Path, doc: dict, original: bytes | None) -> str | None:
    """Back up ``original`` (when the file existed) and atomically replace the
    file with ``doc``. Returns the backup path.

    A symlinked hook file (dotfiles, stow) stays a symlink: the temp file,
    mode, backup and rename all use the link's real target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    # The file must not have changed since it was read (an agent may be
    # saving its settings right now): a lost update is worse than a retry.
    try:
        current = path.read_bytes()
    except FileNotFoundError:
        current = None
    if current != original:
        raise HookFileError(f"{path} changed while it was being edited; try again")
    backup = None
    mode = 0o600
    if original is not None:
        mode = path.stat().st_mode & 0o777
        backup_file = _backup_path(path)
        fd = os.open(backup_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(original)
        backup = str(backup_file)
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return backup


def apply(
    action: str,
    agents: Iterable[str] = AGENTS,
    *,
    home: Path | None = None,
    hook_path: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict:
    """Run ``action`` for ``agents``. Returns ``{"action", "ok", "hook_path",
    "agents": {agent: status + {"changed", "backup"}}}``; ``ok`` is false when
    any agent reports an ``error``."""
    if action not in ACTIONS:
        raise ValueError(f"unknown action: {action}")
    agents = list(dict.fromkeys(agents))
    for agent in agents:
        if agent not in AGENTS:
            raise ValueError(f"unknown agent: {agent}")
    home = Path.home() if home is None else home
    resolved = resolve_hook_path(hook_path) if action == "install" else None
    results: dict[str, dict] = {}
    with _WRITE_LOCK:
        for agent in agents:
            changed, backup, error = False, None, None
            if action != "status":
                path = hook_file(agent, home, env)
                try:
                    if action == "install" and resolved is None:
                        raise HookFileError(
                            f"{HOOK_NAME} not found (next to {sys.executable} or on PATH); "
                            "pass --hook-path"
                        )
                    doc, raw = _load(path)
                    base = doc if doc is not None else {}
                    if action == "install":
                        new = _installed(base, agent, hook_command(resolved, agent))
                    else:
                        new = _uninstalled(base)
                    if new != base and (doc is not None or action == "install"):
                        backup = _write(path, new, raw)
                        changed = True
                except (HookFileError, OSError) as exc:
                    error = str(exc) if isinstance(exc, HookFileError) else f"{path}: {exc}"
            status = agent_status(agent, home, env)
            if error is not None:
                status["error"] = error
            results[agent] = {**status, "changed": changed, "backup": backup}
    return {
        "action": action,
        "ok": all(r["error"] is None for r in results.values()),
        "hook_path": resolved if action == "install" else resolve_hook_path(hook_path),
        "agents": results,
    }


def summary(agents: Mapping[str, object]) -> dict:
    """The compact per-agent view GET /maintenance carries."""
    out = {}
    for agent, raw in agents.items():
        if agent not in AGENTS or not isinstance(raw, dict):
            continue
        item = {
            "installed": raw.get("installed") is True,
            "file": raw.get("file") if isinstance(raw.get("file"), str) else None,
            "error": raw.get("error") if isinstance(raw.get("error"), str) else None,
        }
        if agent == "codex":
            enabled = raw.get("features_hooks_enabled")
            item["needs_trust"] = raw.get("needs_trust") is True
            item["features_hooks_enabled"] = enabled if isinstance(enabled, bool) else None
        out[agent] = item
    return out


# --- bridge message -------------------------------------------------------------


def _parse_agents(raw: object) -> list[str] | None:
    if raw is None:
        return list(AGENTS)
    if isinstance(raw, str):
        raw = [a.strip() for a in raw.split(",") if a.strip()]
    if not isinstance(raw, list) or not raw or not all(isinstance(a, str) for a in raw):
        return None
    if any(a not in AGENTS for a in raw):
        return None
    return list(dict.fromkeys(raw))


async def bridge_reply(msg: dict, *, timeout: float = BRIDGE_TIMEOUT_S, **kwargs) -> dict:
    """Answer ``{"type": "hooks", "req", "action", "agents"}`` (full token
    only — the caller enforces that). File IO runs off the event loop."""
    req = msg.get("req")
    req = req if isinstance(req, str) else ""
    action = msg.get("action", "status")
    agents = _parse_agents(msg.get("agents"))
    if action not in ACTIONS or agents is None:
        return {"type": "error", "req": req, "message": "hooks: invalid action or agents"}
    try:
        data = await asyncio.wait_for(asyncio.to_thread(apply, action, agents, **kwargs), timeout)
    except TimeoutError:
        return {"type": "error", "req": req, "message": "hooks: timed out"}
    except Exception as exc:  # noqa: BLE001 - reported to the client, never fatal
        return {"type": "error", "req": req, "message": f"hooks: {exc}"}
    return {"type": "result", "req": req, "data": data}


# --- CLI (herdeck-service hooks ...) ---------------------------------------------


def _describe(result: dict) -> list[str]:
    lines = []
    for agent, r in result["agents"].items():
        name = "Claude Code" if agent == "claude" else "Codex"
        if r["error"]:
            state = f"error: {r['error']}"
        elif r["installed"]:
            state = "installed"
        elif r["events"]:
            state = f"partly installed ({', '.join(r['events'])})"
        else:
            state = "not installed"
        lines.append(f"{name}: {state} [{r['file']}]")
        if r.get("config_dir_source") not in (None, "home"):
            lines.append(f"  config directory from ${r['config_dir_source']}")
        if r.get("backup"):
            lines.append(f"  backup: {r['backup']}")
        if agent == "codex" and r["events"]:
            if r.get("features_hooks_enabled") is not True:
                lines.append(
                    "  Codex runs hooks only with `[features] hooks = true` in "
                    f"{Path(r['file']).with_name('config.toml')} (not changed by herdeck)"
                )
            lines.append("  Codex asks to trust new hooks: run /hooks in a Codex session")
        if r.get("changed"):
            lines.append("  running agents pick the change up after a restart")
    return lines


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="herdeck-service hooks",
        description="Install, remove or inspect the herdeck-subagent-hook agent hooks.",
    )
    p.add_argument("action", choices=ACTIONS)
    p.add_argument("--agents", default=",".join(AGENTS), help="comma list: claude,codex")
    p.add_argument("--hook-path", default=None, help=f"path of {HOOK_NAME} (default: auto)")
    p.add_argument("--home", type=Path, default=None, help=argparse.SUPPRESS)
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    agents = _parse_agents(args.agents)
    if agents is None:
        parser().error(f"--agents: choose from {', '.join(AGENTS)}")
    result = apply(args.action, agents, home=args.home, hook_path=args.hook_path)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("\n".join(_describe(result)))
    return 0 if result["ok"] else 1
