"""CLI connection setup: local T3 auth -> keychain -> backed-up config.

T3 CLI must already be installed. No provider credentials are read or copied.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import tomli_w

from .secrets import clear_secret, get_secret, peek_keychain, set_secret
from .settings import SettingsSnapshot, _merged_sections, resolve_profile
from .t3 import T3Http, validate_shell


def discover(base_dir: Path) -> str:
    state = json.loads((base_dir / "userdata/server-runtime.json").read_text())
    host, port = state["host"], state["port"]
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    url = f"http://{host}:{port}"
    T3Http(url, "")  # Validate discovery before any credential leaves the process.
    return url


def updated_config(data: dict, sid: str, url: str, token_env: str, profile="default") -> dict:
    result = copy.deepcopy(data)
    servers = result.setdefault("servers", [])
    if any(s["id"] == sid for s in servers):
        raise ValueError("Server ID already exists; choose a new ID")
    servers.append({"id": sid, "url": url, "token_env": token_env, "backend": "t3"})
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


def connect(args):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", args.id):
        raise ValueError("Server ID must contain letters, digits, underscores or hyphens")
    path = args.config
    local_path = Path(os.environ.get("HERDECK_LOCAL_CONFIG", str(path.with_name("local.toml"))))
    marker = path.with_name("onboarding.toml")
    originals = {p: p.read_text() if p.exists() else None for p in (path, local_path, marker)}
    data = tomllib.loads(originals[path] or "")
    local = tomllib.loads(originals[local_path] or "")
    choice = tomllib.loads(originals[marker] or "").get("choice")
    if choice == "demo":
        raise ValueError("Demo mode is selected; choose live mode before connecting T3")
    url = args.url or discover(args.base_dir)
    T3Http(url, "")
    env = "HERDECK_T3_" + args.id.upper().replace("-", "_") + "_TOKEN"
    if get_secret(env) or peek_keychain(env) is not None:
        raise ValueError("Credential name already exists; choose a new server ID")
    profile = os.environ.get("HERDECK_PROFILE") or local.get("active_profile") or data.get("active_profile") or "default"
    proposed = updated_config(data, args.id, url, env, profile)
    if choice == "local" and "herdr_sessions" not in local.get("local", {}):
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
    issued = json.loads(subprocess.check_output([args.binary, "auth", "session", "issue",
        "--base-dir", str(args.base_dir), "--json", "--ttl", "30d", "--label", "Herdeck " + args.id],
        text=True, stderr=subprocess.DEVNULL, timeout=30))
    token = issued["token"]
    written = {}
    stored = False
    try:
        snapshot = T3Http(url, token).get("/api/orchestration/shell")
        validate_shell(snapshot)
        set_secret(env, token)
        stored = True
        resolved = resolve_profile(SettingsSnapshot(path, local_path, proposed, local, os.environ.get("HERDECK_PROFILE")))
        if args.id not in [s.id for s in resolved.config.servers]:
            raise ValueError("Active profile excludes T3")
        if any((p.read_text() if p.exists() else None) != old for p, old in originals.items()):
            raise ValueError("Configuration changed concurrently; retry setup")
        for p, content in ((path, tomli_w.dumps(proposed)), (local_path, tomli_w.dumps(local))):
            atomic_write(p, content)
            written[p] = content
        if choice == "local":
            marker.unlink()
    except Exception:
        for p, content in written.items():
            if p.exists() and p.read_text() == content:
                if originals[p] is None:
                    p.unlink()
                else:
                    atomic_write(p, originals[p])
        if stored:
            clear_secret(env)
        subprocess.run([args.binary, "auth", "session", "revoke", issued["sessionId"],
            "--base-dir", str(args.base_dir)], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=30, check=False)
        raise
    print(json.dumps({"server_id": args.id, "url": url, "threads": len(snapshot["threads"]),
                      "profile": resolved.config.meta.active_profile, "backups": backups,
                      "session_id": issued["sessionId"], "expires_at": issued["expiresAt"]}))


def main():
    p = argparse.ArgumentParser(description="Connect an existing local T3 server to Herdeck")
    p.add_argument("--config", type=Path, default=Path(os.environ.get("HERDECK_CONFIG", str(Path.home() / ".config/herdeck/config.toml"))))
    p.add_argument("--base-dir", type=Path, default=Path.home() / ".t3")
    p.add_argument("--binary", default="t3", help="Path to the T3 CLI executable")
    p.add_argument("--url", help="Override discovered T3 HTTP origin")
    p.add_argument("--id", default="t3-local")
    p.add_argument("--disconnect", action="store_true", help="Remove the connection; retain its credential for rollback")
    p.add_argument("--renew", action="store_true", help="Renew the existing ID within seven days of expiry")
    p.add_argument("--issuer-ssh", help="Existing SSH host of the T3 boot service")
    p.add_argument("--restart-label", help="Restart this GUI launchd runtime after successful renewal")
    args = p.parse_args()
    try:
        if args.renew and args.disconnect:
            raise ValueError("Choose either renewal or disconnect")
        if args.renew:
            from .t3_renew import renew
            renew(args)
        elif args.disconnect:
            disconnect(args)
        else:
            connect(args)
    except Exception as exc:
        # Subprocess failures may contain command output. Never render those.
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        p.exit(1, "T3 setup failed: " + message + "\n")


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
    print(json.dumps({"disconnected": args.id, "backup": str(backup), "credential_retained": True}))


if __name__ == "__main__":
    main()
