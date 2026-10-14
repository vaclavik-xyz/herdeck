"""Renew the same T3 connection via its issuer; secrets remain in Keychain.

The issuing host must already trust the caller's SSH key. This does not install
keys or create network routes. launchd must run it in the user's GUI domain so
it can access the same Keychain as the Herdeck runtime.
"""
import json
import os
import re
import shutil
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from .secrets import peek_keychain, set_secret
from .t3_actions import negotiate
from .t3_client import T3Client, T3Error, T3Http
from .t3_state import timestamp

SCOPES = ("--scope", "orchestration:read", "--scope", "orchestration:operate")
_VERSION = r"[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?"

# Read the installed boot-service version instead of downloading a moving npm tag.
# Runs remotely via `python3 -c`, so it is self-contained; keep the CLI selection
# in step with _cli_path below (T3 0.0.42+ ships a standalone runtime/versions/<v>/t3).
_REMOTE = '''import json, pathlib, re, shutil, subprocess, sys
p = json.loads(sys.stdin.readline())
base = pathlib.Path.home() / ".t3"
version = json.loads((base / "runtime/service-state.json").read_text())["activeVersion"]
if not re.fullmatch(r"[0-9]+\\.[0-9]+\\.[0-9]+(-[0-9A-Za-z.]+)?", version): raise ValueError("Invalid runtime version")
vdir = base / "runtime/versions" / version
exe = vdir / "t3"
legacy = vdir / "node_modules/t3/dist/bin.mjs"
if exe.is_file():
    cli = [str(exe)]
elif legacy.is_file():
    node = "/opt/homebrew/bin/node" if pathlib.Path("/opt/homebrew/bin/node").exists() else shutil.which("node")
    cli = [node, str(legacy)]
else: raise ValueError("T3 runtime CLI not found")
args = cli + ["auth", "session", p["operation"]]
if p["operation"] == "issue": args += ["--json", "--scope", "orchestration:read", "--scope", "orchestration:operate", "--ttl", "30d", "--label", p["label"]]
else: args += [p["sessionId"]]
args += ["--base-dir", str(base)]
result = subprocess.run(args, capture_output=True, text=True, timeout=30, check=True)
if p["operation"] == "issue": print(result.stdout, end="")
'''


def _cli_path(base: Path, version: str) -> list[str]:
    """argv prefix of the T3 CLI installed under ``base`` for ``version``."""
    if not re.fullmatch(_VERSION, version):
        raise ValueError("Invalid runtime version")
    vdir = base / "runtime/versions" / version
    exe = vdir / "t3"
    if exe.is_file():
        return [str(exe)]
    legacy = vdir / "node_modules/t3/dist/bin.mjs"
    if legacy.is_file():
        node = "/opt/homebrew/bin/node" if Path("/opt/homebrew/bin/node").exists() else shutil.which("node")
        if node:
            return [node, str(legacy)]
    raise ValueError("T3 runtime CLI not found")


def _local_cli(args) -> list[str]:
    state = Path(args.base_dir) / "runtime/service-state.json"
    if state.is_file():
        return _cli_path(Path(args.base_dir), json.loads(state.read_text())["activeVersion"])
    return [args.binary]


def issuer(args, operation, session_id=None):
    if args.issuer_ssh:
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*", args.issuer_ssh):
            raise ValueError("Invalid SSH issuer")
        # Remote shell sees only fixed program text. Parameters go through stdin.
        import shlex
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", args.issuer_ssh,
                   "python3 -c " + shlex.quote(_REMOTE)]
        result = subprocess.run(command, input=json.dumps(dict(operation=operation,
            label="Herdeck " + args.id, sessionId=session_id)) + "\n",
            capture_output=True, text=True, timeout=45, check=True)
        return json.loads(result.stdout) if operation == "issue" else None
    command = [*_local_cli(args), "auth", "session", operation]
    command += (["--json", *SCOPES, "--ttl", "30d", "--label", "Herdeck " + args.id]
                if operation == "issue" else [session_id])
    command += ["--base-dir", str(args.base_dir)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout) if operation == "issue" else None


def renew(args):
    original = args.config.read_text()
    data = tomllib.loads(original)
    server = next((s for s in data.get("servers", []) if s["id"] == args.id and s.get("backend") == "t3"), None)
    if server is None:
        raise ValueError("No T3 connection with this ID")
    if not args.restart_label:
        raise ValueError("Renewal requires --restart-label so the running runtime adopts the new credential")
    env = server.get("token_env")
    if not env:
        raise ValueError("Renewal requires a Keychain credential (token_env); token_file servers are renewed by hand")
    if os.environ.get(env):
        raise ValueError("Renewal requires Keychain credentials; remove the environment override")
    old = peek_keychain(env)  # An unreadable Keychain must never be overwritten.
    if not old:
        raise ValueError("Existing Keychain credential is missing")
    if args.restart_label and not re.fullmatch(r"[A-Za-z0-9_.-]+", args.restart_label):
        raise ValueError("Invalid runtime launchd label")
    expires = None
    try:
        session = T3Http(server["url"], old).get("/api/auth/session")
        expires = timestamp(session.get("expiresAt"))
    except T3Error as exc:
        if exc.code != 401:
            raise
    if expires and expires - datetime.now(UTC).timestamp() > 7 * 86400:
        print(json.dumps(dict(server_id=args.id, renewed=False, expires_at=session["expiresAt"])))
        return
    issued = issuer(args, "issue")
    stored = False
    try:
        client = T3Client(server["url"], issued["token"])
        negotiate(client.descriptor())
        client.shell()
        if args.config.read_text() != original:
            raise ValueError("Configuration changed concurrently")
        if peek_keychain(env) != old:
            raise ValueError("Credential changed concurrently")
        set_secret(env, issued["token"])
        stored = True
        if args.restart_label:
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{args.restart_label}"],
                           check=True, capture_output=True, timeout=30)
    except Exception:
        if stored:
            set_secret(env, old)
        issuer(args, "revoke", issued["sessionId"])
        raise
    # The old session expires naturally; keeping it briefly permits rollback.
    print(json.dumps(dict(server_id=args.id, renewed=True, expires_at=issued["expiresAt"],
                          runtime_restarted=bool(args.restart_label))))
