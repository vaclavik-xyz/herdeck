"""CLI connection setup: T3 auth -> keychain (or a token file) -> backed-up config.

Connects a T3 server on this Mac (local T3 CLI) or, with ``--issuer-ssh``, on
another host reached through an SSH local forward (token issued over SSH).
T3 must already be installed. No provider credentials are read or copied.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import tomli_w

from . import t3_remote
from .secrets import clear_secret, get_secret, peek_keychain, set_secret, write_token_file
from .settings import SettingsSnapshot, _merged_sections, resolve_profile
from .t3_actions import Incompatible, negotiate
from .t3_client import T3Client, T3Http, local_runtime_origin


def discover(base_dir: Path) -> str:
    # Validated before any credential leaves the process.
    return local_runtime_origin(base_dir)


def updated_config(data: dict, sid: str, url: str, token_env: str | None, profile="default", extra=None) -> dict:
    result = copy.deepcopy(data)
    servers = result.setdefault("servers", [])
    if any(s["id"] == sid for s in servers):
        raise ValueError("Server ID already exists; choose a new ID")
    credential = {"token_env": token_env} if token_env else {}
    servers.append({"id": sid, "url": url, **credential, "backend": "t3", **(extra or {})})
    # Override only the active profile, leaving other explicit selections intact.
    merged, selection = _merged_sections(data, profile)
    selection = selection if selection is not None else (merged.get("deck") or {}).get("overview_order")
    if selection is not None:
        if profile == "default":
            result.setdefault("deck", {})["overview_order"] = [*selection, sid]
        else:
            result["profiles"][profile]["servers"] = [*selection, sid]
    return result


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def token_env_for(sid: str) -> str:
    return "HERDECK_T3_" + sid.upper().replace("-", "_") + "_TOKEN"


class _Plan:
    """Everything a connect reads and checks before its first side effect."""

    def __init__(self, args):
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", args.id):
            raise ValueError("Server ID must contain letters, digits, underscores or hyphens")
        self.args = args
        self.path = path = args.config
        self.local_path = Path(os.environ.get("HERDECK_LOCAL_CONFIG", str(path.with_name("local.toml"))))
        self.marker = path.with_name("onboarding.toml")
        self.originals = {p: p.read_text() if p.exists() else None for p in (path, self.local_path, self.marker)}
        self.data = tomllib.loads(self.originals[path] or "")
        self.local = tomllib.loads(self.originals[self.local_path] or "")
        self.choice = tomllib.loads(self.originals[self.marker] or "").get("choice")
        if self.choice == "demo":
            raise ValueError("Demo mode is selected; choose live mode before connecting T3")
        if any(s.get("id") == args.id for s in self.data.get("servers", [])):
            raise ValueError("Server ID already exists; choose a new ID")
        token_file = getattr(args, "token_file", None)
        if token_file is not None:
            # A token file needs no Keychain: setup works over SSH and an
            # ad-hoc signed runtime reads it without an access prompt.
            self.env = None
            # Absolute: the runtime and the renew agent run with another cwd.
            self.token_file = (Path(os.path.expanduser(token_file)) if token_file
                               else path.with_name(args.id + "-token")).absolute()
            # A dangling symlink would be written through to its target.
            if self.token_file.exists() or self.token_file.is_symlink():
                raise ValueError(f"Token file {self.token_file} already exists; choose another path or ID")
        else:
            self.token_file = None
            self.env = token_env_for(args.id)
            if get_secret(self.env) or peek_keychain(self.env) is not None:
                raise ValueError("Credential name already exists; choose a new server ID")
        self.profile = (os.environ.get("HERDECK_PROFILE") or self.local.get("active_profile")
                        or self.data.get("active_profile") or "default")


def _commit(plan: _Plan, url: str, issue, revoke, extra=None) -> dict:
    """The connect transaction shared by local and remote setup.

    Backs up the config files, ``issue()``s a scoped session (returns the T3
    session JSON), verifies it against ``url``, stores it in Keychain (or the
    plan's token file) and writes the config. Any failure after issuing
    restores the config files, clears the stored credential (with directories
    created for the token file) and ``revoke(session_id)``s the session (best
    effort)."""
    args, path, local_path, local = plan.args, plan.path, plan.local_path, plan.local
    originals, env = plan.originals, plan.env
    T3Http(url, "")
    if plan.token_file is not None:
        extra = {**(extra or {}), "token_file": str(plan.token_file)}
    proposed = updated_config(plan.data, args.id, url, env, plan.profile, extra)
    if plan.choice == "local" and "herdr_sessions" not in local.get("local", {}):
        from .deckapp.sessions import discover_local_sessions
        # Retain the effective local selection when removing the local-only marker.
        sessions = discover_local_sessions(local_path)
        local.setdefault("local", {})["herdr_sessions"] = [s.name for s in sessions if s.selected]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backups = []
    for p, original in originals.items():
        if original is not None:
            backup = p.with_name(p.name + "." + stamp + ".bak")
            shutil.copy2(p, backup)
            backups.append(str(backup))
    # Token file parents write_token_file will create, deepest first.
    new_dirs = ([d for d in plan.token_file.parents if not d.exists()]
                if plan.token_file is not None else [])
    issued = issue()
    written = {}
    stored = False
    try:
        client = T3Client(url, issued["token"])
        try:
            negotiate(client.descriptor())
        except Incompatible as exc:
            raise ValueError(str(exc)) from None
        snapshot = client.shell()
        if plan.token_file is not None:
            stored = True  # Set before writing: a partial write is cleaned up too.
            write_token_file(plan.token_file, issued["token"])
        else:
            set_secret(env, issued["token"])
            stored = True
        resolved = resolve_profile(SettingsSnapshot(path, local_path, proposed, local, os.environ.get("HERDECK_PROFILE")))
        if args.id not in [s.id for s in resolved.config.servers]:
            raise ValueError("Active profile excludes T3")
        if any((p.read_text() if p.exists() else None) != old for p, old in originals.items()):
            raise ValueError("Configuration changed concurrently; retry setup")
        for p, content in ((path, tomli_w.dumps(proposed)), (local_path, tomli_w.dumps(local))):
            atomic_write(p, content)
            written[p] = content
        if plan.choice == "local":
            plan.marker.unlink()
    except Exception:
        for p, content in written.items():
            if p.exists() and p.read_text() == content:
                if originals[p] is None:
                    p.unlink()
                else:
                    atomic_write(p, originals[p])
        if stored:
            if plan.token_file is not None:
                plan.token_file.unlink(missing_ok=True)
                for d in new_dirs:
                    try:
                        d.rmdir()  # Only while empty: never removes other files.
                    except OSError:
                        break
            else:
                clear_secret(env)
        try:
            revoke(issued["sessionId"])
        except Exception:
            pass  # Best effort (the session still expires); never mask the cause.
        raise
    result = {"server_id": args.id, "url": url, "threads": len(snapshot["threads"]),
              "profile": resolved.config.meta.active_profile, "backups": backups,
              "session_id": issued["sessionId"], "expires_at": issued["expiresAt"]}
    if plan.token_file is not None:
        result["token_file"] = str(plan.token_file)
    return result


def _checked_session(issued) -> dict:
    issued["token"]
    issued["sessionId"]
    return issued


def connect(args):
    if getattr(args, "issuer_ssh", None):
        return connect_remote(args)
    plan = _Plan(args)
    url = args.url or discover(args.base_dir)
    from .t3_renew import _local_cli
    cli = _local_cli(args)

    def issue():
        try:
            return _checked_session(json.loads(subprocess.check_output([*cli, "auth", "session", "issue",
                "--base-dir", str(args.base_dir), "--json",
                "--scope", "orchestration:read", "--scope", "orchestration:operate", "--ttl", "30d", "--label", "Herdeck " + args.id],
                text=True, stderr=subprocess.DEVNULL, timeout=30)))
        except Exception:
            # Never include command output: it may contain a token.
            raise ValueError(f"Could not issue a scoped T3 session with {' '.join(cli)}; "
                             "T3 0.0.46+ is required for scoped tokens (pass --binary to choose the CLI)") from None

    def revoke(session_id):
        subprocess.run([*cli, "auth", "session", "revoke", session_id,
            "--base-dir", str(args.base_dir)], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=30, check=False)

    print(json.dumps(_commit(plan, url, issue, revoke)))


def connect_remote(args):
    """Connect a T3 server on another host, reached over an SSH local forward.

    The token is issued (and on failure revoked) on the T3 host over SSH via
    the renewal issuer. With ``--install-forward`` the per-server forward
    LaunchAgent is installed first and removed again if the connect fails."""
    from .t3_renew import issuer

    target = t3_remote.validate_ssh_target(args.issuer_ssh)
    if getattr(args, "url", None):
        raise ValueError("--url and --issuer-ssh are exclusive; the URL is the local SSH forward")
    plan = _Plan(args)
    install = getattr(args, "install_forward", False)
    local_port = getattr(args, "local_port", None)
    remote_port = getattr(args, "remote_port", None)
    if local_port is not None:
        t3_remote.check_port(local_port, "--local-port")
        if local_port in t3_remote.used_ports(plan.data):
            raise ValueError(f"Port {local_port} is already used by another configured server")
    if remote_port is not None:
        t3_remote.check_port(remote_port, "--remote-port")
    installed = False
    try:
        if install:
            label = t3_remote.forward_label(args.id)
            if (t3_remote.agents_dir() / f"{label}.plist").exists():
                raise ValueError(f"LaunchAgent {label} already exists; run --disconnect first or omit --install-forward")
            remote_host = "127.0.0.1"
            if remote_port is None:
                remote_host, remote_port = t3_remote.remote_runtime(target)
            if local_port is None:
                local_port = t3_remote.pick_port(plan.data)
            elif not t3_remote.port_free(local_port):
                raise ValueError(f"Port {local_port} is already in use on this Mac")
            t3_remote.install_forward(args.id, target, local_port, remote_port, remote_host)
            installed = True
            t3_remote.wait_for_port(local_port)
        else:
            if local_port is None:
                raise ValueError("Pass --local-port of the existing SSH forward, or --install-forward")
            if not t3_remote.port_answers(local_port):
                raise ValueError(f"Nothing answers on 127.0.0.1:{local_port}; start the SSH forward "
                                 "or pass --install-forward")

        def issue():
            try:
                return _checked_session(issuer(args, "issue"))
            except Exception:
                # Never include command output: it may contain a token.
                raise ValueError(f"Could not issue a scoped T3 session on {target} over SSH; "
                                 "T3 0.0.46+ must run there and accept this SSH key") from None

        def revoke(session_id):
            issuer(args, "revoke", session_id)

        result = _commit(plan, f"http://127.0.0.1:{local_port}", issue, revoke, {"issuer_ssh": target})
    except Exception:
        if installed:
            t3_remote.remove_forward(args.id)
        raise
    result["issuer_ssh"] = target
    if installed:
        result["forward_agent"] = t3_remote.forward_label(args.id)
    print(json.dumps(result))


def main():
    p = argparse.ArgumentParser(description="Connect a T3 server (this Mac, or remote over SSH) to Herdeck")
    p.add_argument("--config", type=Path, default=Path(os.environ.get("HERDECK_CONFIG", str(Path.home() / ".config/herdeck/config.toml"))))
    p.add_argument("--base-dir", type=Path, default=Path.home() / ".t3")
    p.add_argument("--binary", default=None, help="Path to the T3 CLI executable (default: the runtime CLI under --base-dir, else t3 on PATH)")
    p.add_argument("--url", help="Override discovered T3 HTTP origin (local connect only)")
    p.add_argument("--id", default="t3-local")
    p.add_argument("--disconnect", action="store_true",
                   help="Remove the connection (and a forward agent this tool installed); retain its credential for rollback")
    p.add_argument("--renew", action="store_true", help="Renew the existing ID within seven days of expiry")
    p.add_argument("--renew-all", action="store_true",
                   help="Renew every T3 server due for renewal (remote ones via their issuer_ssh); restart the runtime once")
    p.add_argument("--issuer-ssh", help="user@host of the T3 host: connect it remotely, or the issuer for --renew")
    p.add_argument("--local-port", type=int, help="Loopback port of the SSH forward (default with --install-forward: first free from 13773)")
    p.add_argument("--remote-port", type=int, help="T3 port on the remote host (default: read from its server-runtime.json)")
    p.add_argument("--install-forward", action="store_true",
                   help=f"macOS: install the LaunchAgent {t3_remote.FORWARD_PREFIX}<id> running the SSH forward")
    p.add_argument("--token-file", nargs="?", const="", default=None, metavar="PATH",
                   help="Store the token in an owner-only file instead of Keychain "
                        "(default path: <config dir>/<id>-token); works over SSH and with unsigned builds")
    p.add_argument("--restart-label", help="Restart this GUI launchd runtime after successful renewal")
    p.add_argument("--install-renew-agent", action="store_true",
                   help=f"macOS: install {t3_remote.RENEW_LABEL} running --renew-all hourly (needs --restart-label)")
    p.add_argument("--remove-renew-agent", action="store_true",
                   help=f"macOS: unload and delete the {t3_remote.RENEW_LABEL} agent this tool installed")
    args = p.parse_args()
    try:
        modes = [m for m in ("renew", "renew_all", "disconnect", "install_renew_agent", "remove_renew_agent")
                 if getattr(args, m)]
        if len(modes) > 1:
            raise ValueError("Choose one of --renew, --renew-all, --disconnect, --install-renew-agent, "
                             "--remove-renew-agent")
        if args.renew:
            from .t3_renew import renew
            renew(args)
        elif args.renew_all:
            from .t3_renew import renew_all
            if not renew_all(args):
                sys.exit(1)
        elif args.disconnect:
            disconnect(args)
        elif args.install_renew_agent:
            install_renew_agent(args)
        elif args.remove_renew_agent:
            remove_renew_agent()
        else:
            connect(args)
    except Exception as exc:
        # Subprocess failures may contain command output. Never render those.
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        p.exit(1, "T3 setup failed: " + message + "\n")


def install_renew_agent(args):
    if not args.restart_label:
        raise ValueError("--install-renew-agent needs --restart-label (the runtime's launchd label)")
    path = t3_remote.install_renew_agent(args.config.expanduser().resolve(), args.restart_label)
    print(json.dumps({"renew_agent": t3_remote.RENEW_LABEL, "plist": str(path)}))


def remove_renew_agent():
    removed = t3_remote.remove_renew_agent()
    print(json.dumps({"renew_agent": t3_remote.RENEW_LABEL, "removed": removed}))


def disconnect(args):
    path = args.config
    original = path.read_text()
    data = tomllib.loads(original)
    server = next((s for s in data.get("servers", []) if s["id"] == args.id), None)
    if server is None or server.get("backend") != "t3":
        raise ValueError("No T3 connection with this ID")
    data["servers"] = [s for s in data["servers"] if s["id"] != args.id]
    for section in [data, *data.get("profiles", {}).values()]:
        if section is not data and isinstance(section.get("servers"), list):
            section["servers"] = [s for s in section["servers"] if s != args.id]
        if "overview_order" in section.get("deck", {}):
            section["deck"]["overview_order"] = [s for s in section["deck"]["overview_order"] if s != args.id]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(path.name + "." + stamp + ".bak")
    shutil.copy2(path, backup)
    if path.read_text() != original:
        raise ValueError("Configuration changed concurrently; retry")
    atomic_write(path, tomli_w.dumps(data))
    # Only the per-server label this tool installs; a hand-made forward is left alone.
    result = {"disconnected": args.id, "backup": str(backup), "credential_retained": True}
    try:
        result["forward_agent_removed"] = (re.fullmatch(r"[a-zA-Z0-9_-]+", args.id) is not None
                                           and t3_remote.remove_forward(args.id))
    except Exception as exc:
        # The config change is committed; report the leftover agent instead of failing.
        result["forward_agent_removed"] = False
        result["warning"] = f"could not remove {t3_remote.forward_label(args.id)} ({type(exc).__name__})"
    print(json.dumps(result))


if __name__ == "__main__":
    main()
