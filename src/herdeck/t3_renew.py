"""Renew the same T3 connection via its issuer.

The credential is renewed where the runtime reads it: the server's
``token_file`` when one is configured (the runtime prefers it to the Keychain),
else the Keychain entry named ``token_env``.

The issuing host must already trust the caller's SSH key. This does not install
keys or create network routes. launchd must run it in the user's GUI domain so
it can access the same Keychain as the Herdeck runtime.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from .config import ConfigError
from .secrets import peek_keychain, set_secret, write_token_file
from .t3_actions import negotiate
from .t3_client import T3Client, T3Error, T3Http
from .t3_remote import validate_ssh_target
from .t3_state import timestamp

SCOPES = ("--scope", "orchestration:read", "--scope", "orchestration:operate")
# Mirrors upstream EXACT_SERVICE_VERSION (serviceProtocol.ts): exact SemVer only.
_VERSION = r"(?:0|[1-9][0-9]*)[.](?:0|[1-9][0-9]*)[.](?:0|[1-9][0-9]*)(?:-(?:(?:0|[1-9][0-9]*)|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(?:[.](?:(?:0|[1-9][0-9]*)|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?(?:[+][0-9A-Za-z-]+(?:[.][0-9A-Za-z-]+)*)?"

# Read the installed boot-service version instead of downloading a moving npm tag.
# Runs remotely via `python3 -c`, so it is self-contained; keep the CLI selection
# in step with _cli_path below (T3 0.0.42+ ships a standalone runtime/versions/<v>/t3).
_REMOTE = '''import json, pathlib, re, shutil, subprocess, sys
p = json.loads(sys.stdin.readline())
base = pathlib.Path.home() / ".t3"
version = json.loads((base / "runtime/service-state.json").read_text())["activeVersion"]
if not re.fullmatch(r"(?:0|[1-9][0-9]*)[.](?:0|[1-9][0-9]*)[.](?:0|[1-9][0-9]*)(?:-(?:(?:0|[1-9][0-9]*)|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(?:[.](?:(?:0|[1-9][0-9]*)|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?(?:[+][0-9A-Za-z-]+(?:[.][0-9A-Za-z-]+)*)?", version): raise ValueError("Invalid runtime version")
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
    """Resolve the T3 CLI: explicit --binary, else the runtime CLI, else ``t3`` on PATH."""
    explicit = getattr(args, "binary", None)
    if explicit:
        return [explicit]
    state = Path(args.base_dir) / "runtime/service-state.json"
    if state.is_file():
        return _cli_path(Path(args.base_dir), json.loads(state.read_text())["activeVersion"])
    return ["t3"]


def issuer(args, operation, session_id=None):
    if args.issuer_ssh:
        try:
            validate_ssh_target(args.issuer_ssh)
        except ValueError:
            raise ValueError("Invalid SSH issuer") from None
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


RENEW_WINDOW = 7 * 86400


class _Credential:
    """Where the runtime reads a server's token, so renewal replaces that one:
    its ``token_file`` when the file exists (the runtime reads it before the
    Keychain, so renewing only the Keychain would leave the runtime on a revoked
    token), else the Keychain entry named ``token_env``. Raises when the server
    cannot be renewed."""

    def __init__(self, server: dict):
        env = server.get("token_env")
        path = server.get("token_file")
        if env and os.environ.get(env):
            raise ValueError("Renewal cannot replace an environment override; remove it")
        path = Path(os.path.expanduser(path.strip())) if isinstance(path, str) and path.strip() else None
        # Same order as the runtime: an absent token_file falls through to the Keychain.
        if path is not None and (path.exists() or not env):
            self.path, self.env = path, None
        elif env:
            self.path, self.env = None, env
        else:
            raise ValueError("Renewal requires token_env or token_file")
        self.server_id = str(server.get("id"))

    def read(self) -> str | None:
        """The stored token, None when absent; raises when unreadable, so an
        unreadable credential is never overwritten."""
        if self.path is None:
            return peek_keychain(self.env)
        from .settings import _read_token_file
        try:
            return _read_token_file(self.server_id, self.path)
        except ConfigError as exc:
            raise ValueError(str(exc)) from None

    def write(self, token: str) -> None:
        if self.path is None:
            set_secret(self.env, token)
            return
        write_token_file(self.path, token)


def _check_label(label):
    if label and not re.fullmatch(r"[A-Za-z0-9_.-]+", label):
        raise ValueError("Invalid runtime launchd label")


def _restart(label):
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
                   check=True, capture_output=True, timeout=30)


def _renew_one(args, server: dict, original: str, restart_label=None) -> dict:
    """Renew one server if it is within the renewal window.

    ``args`` carries ``id``, ``issuer_ssh`` (None = local CLI), ``base_dir``,
    ``binary`` and ``config``. With ``restart_label`` the runtime is restarted
    inside the transaction (a failed restart rolls the credential back);
    without it the caller restarts. Returns the JSON-safe result (no tokens)."""
    credential = _Credential(server)
    old = credential.read()  # An unreadable credential must never be overwritten.
    if not old:
        raise ValueError("Existing credential is missing")
    expires = None
    try:
        session = T3Http(server["url"], old).get("/api/auth/session")
        expires = timestamp(session.get("expiresAt"))
    except T3Error as exc:
        if exc.code != 401:
            raise
    if expires and expires - datetime.now(UTC).timestamp() > RENEW_WINDOW:
        return dict(server_id=args.id, renewed=False, expires_at=session["expiresAt"])
    issued = issuer(args, "issue")
    stored = False
    try:
        client = T3Client(server["url"], issued["token"])
        negotiate(client.descriptor())
        client.shell()
        if args.config.read_text() != original:
            raise ValueError("Configuration changed concurrently")
        if credential.read() != old:
            raise ValueError("Credential changed concurrently")
        credential.write(issued["token"])
        stored = True
        if restart_label:
            _restart(restart_label)
    except Exception:
        if stored:
            credential.write(old)
        issuer(args, "revoke", issued["sessionId"])
        raise
    # The old session expires naturally; keeping it briefly permits rollback.
    return dict(server_id=args.id, renewed=True, expires_at=issued["expiresAt"])


def renew(args):
    original = args.config.read_text()
    data = tomllib.loads(original)
    server = next((s for s in data.get("servers", []) if s["id"] == args.id and s.get("backend") == "t3"), None)
    if server is None:
        raise ValueError("No T3 connection with this ID")
    if not args.restart_label:
        raise ValueError("Renewal requires --restart-label so the running runtime adopts the new credential")
    _Credential(server)
    _check_label(args.restart_label)
    # An explicit --issuer-ssh (existing launchd jobs) wins over the recorded one.
    args = argparse.Namespace(**{**vars(args), "issuer_ssh": getattr(args, "issuer_ssh", None) or server.get("issuer_ssh")})
    result = _renew_one(args, server, original, args.restart_label)
    print(json.dumps({**result, "runtime_restarted": result["renewed"]}))


def _safe_error(exc: Exception) -> str:
    # Subprocess failures may carry command output (possibly a token): name only.
    return str(exc) if isinstance(exc, (ValueError, T3Error)) else type(exc).__name__


def renew_all(args) -> bool:
    """Renew every ``backend = "t3"`` server due for renewal; one JSON line per
    server on stdout, then a summary. Restarts the runtime once at the end when
    anything was renewed. Returns True when every server succeeded."""
    if getattr(args, "issuer_ssh", None):
        raise ValueError("--renew-all reads each server's issuer_ssh from the config; drop --issuer-ssh")
    _check_label(args.restart_label)
    original = args.config.read_text()
    data = tomllib.loads(original)
    servers = [s for s in data.get("servers", []) if s.get("backend") == "t3"]
    ok = True
    renewed = []
    for server in servers:
        sid = server.get("id")
        one = argparse.Namespace(**{**vars(args), "id": sid, "issuer_ssh": server.get("issuer_ssh")})
        try:
            result = _renew_one(one, server, original)
        except Exception as exc:
            ok = False
            result = dict(server_id=sid, renewed=False, error=_safe_error(exc))
        if result.get("renewed"):
            renewed.append(sid)
        print(json.dumps(result), flush=True)
    restarted = False
    restart_error = None
    if renewed and args.restart_label:
        try:
            _restart(args.restart_label)
            restarted = True
        except Exception as exc:
            # The new credentials are valid and stay stored; the next restart adopts them.
            ok = False
            restart_error = _safe_error(exc)
    summary = dict(servers=len(servers), renewed=renewed, ok=ok, runtime_restarted=restarted)
    if restart_error:
        summary["restart_error"] = restart_error
    elif renewed and not args.restart_label:
        summary["warning"] = "no --restart-label: the running runtime keeps the old credential until restarted"
    print(json.dumps({"summary": summary}), flush=True)
    return ok
