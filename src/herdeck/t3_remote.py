"""Remote T3 plumbing for ``t3_setup``: SSH target validation, loopback port
choice, the per-server SSH local-forward LaunchAgent and remote port discovery.

Nothing here builds a shell command line from user input: SSH targets are
validated strictly and passed as single argv/plist array elements, and the only
remote shell text is a fixed Python program.
"""
from __future__ import annotations

import ipaddress
import json
import os
import plistlib
import re
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

FORWARD_PREFIX = "com.herdeck.t3-forward."
RENEW_LABEL = "com.herdeck.t3-renew"
FIRST_PORT = 13773
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
               "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
               "-o", "ConnectTimeout=8")

_USER = r"[A-Za-z_][A-Za-z0-9_.-]{0,63}"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST = rf"{_LABEL}(?:\.{_LABEL})*"


def validate_ssh_target(value) -> str:
    """``[user@]host`` with a POSIX-ish user and a hostname, alias or IPv4.

    Rejects anything else (options such as ``-oProxyCommand=…``, spaces, shell
    metacharacters, ports, IPv6), so the value is safe as one argv element."""
    if not isinstance(value, str) or len(value) > 255:
        raise ValueError("Invalid SSH target; expected user@host")
    user, _, host = value.rpartition("@")
    if "@" in value and not re.fullmatch(_USER, user):
        raise ValueError("Invalid SSH target; expected user@host")
    if not re.fullmatch(_HOST, host):
        raise ValueError("Invalid SSH target; expected user@host")
    return value


def forward_label(sid: str) -> str:
    return FORWARD_PREFIX + sid


def agents_dir() -> Path:
    return Path.home() / "Library/LaunchAgents"


def logs_dir() -> Path:
    return Path.home() / "Library/Logs/herdeck"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def used_ports(data: dict) -> set[int]:
    """Loopback ports already used by configured servers (any backend)."""
    ports = set()
    for server in data.get("servers", []) or []:
        try:
            url = urlsplit(str(server.get("url", "")))
            if url.hostname in ("127.0.0.1", "localhost", "::1") and url.port:
                ports.add(url.port)
        except ValueError:
            continue
    return ports


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pick_port(data: dict, start: int = FIRST_PORT, attempts: int = 200) -> int:
    """First loopback port from ``start`` that no configured server uses and
    nothing currently listens on."""
    taken = used_ports(data)
    for port in range(start, min(start + attempts, 65536)):
        if port not in taken and port_free(port):
            return port
    raise ValueError("No free loopback port found; pass --local-port")


def port_answers(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(port: int, timeout: float = 30.0, interval: float = 0.5) -> None:
    deadline = time.monotonic() + timeout
    while True:
        if port_answers(port):
            return
        if time.monotonic() >= deadline:
            raise ValueError(f"SSH forward on 127.0.0.1:{port} did not come up; see "
                             f"{logs_dir() / 't3-forward'}*.log")
        time.sleep(interval)


def check_port(value: int, name: str) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError(f"{name} must be a TCP port (1-65535)")
    return value


# Fixed program text run as `python3 -c` on the T3 host; no parameters.
_REMOTE_RUNTIME = '''import json, pathlib
s = json.loads((pathlib.Path.home() / ".t3/userdata/server-runtime.json").read_text())
print(json.dumps({k: s.get(k) for k in ("origin", "host", "port")}))
'''


def remote_runtime(target: str) -> tuple[str, int]:
    """``(forward host, port)`` of the T3 server on ``target``, read from its
    ``~/.t3/userdata/server-runtime.json`` (``origin``/``port``, or the older
    ``host``/``port``). A wildcard or loopback bind is
    forwarded to 127.0.0.1; a specific address is forwarded as reported."""
    validate_ssh_target(target)
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", target,
               "python3 -c " + shlex.quote(_REMOTE_RUNTIME)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=45, check=True)
        state = json.loads(result.stdout)
        # Current T3 writes "origin" (+ "port"); older builds "host" + "port".
        origin = urlsplit(state["origin"]) if isinstance(state.get("origin"), str) and state["origin"] else None
        host = (origin.hostname if origin else None) or state.get("host") or "127.0.0.1"
        port = state.get("port") or (origin.port if origin else None)
    except Exception:
        raise ValueError(f"Could not read the T3 server runtime on {target} over SSH; "
                         "is T3 running there? (or pass --remote-port)") from None
    check_port(port, "remote T3 port")
    if not isinstance(host, str):
        raise ValueError("Remote T3 runtime reported an invalid host")
    if host in ("0.0.0.0", "::", "localhost", ""):
        return "127.0.0.1", port
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(_HOST, host):
            raise ValueError("Remote T3 runtime reported an invalid host") from None
        return host, port
    if addr.is_loopback:
        return ("127.0.0.1" if addr.version == 4 else "[::1]"), port
    if addr.version == 6:
        return f"[{host}]", port
    return host, port


def forward_plist(sid: str, target: str, local_port: int, remote_port: int,
                  remote_host: str = "127.0.0.1") -> dict:
    validate_ssh_target(target)
    check_port(local_port, "--local-port")
    check_port(remote_port, "--remote-port")
    label = forward_label(sid)
    log = str(logs_dir() / f"t3-forward.{sid}.log")
    return {
        "Label": label,
        "ProgramArguments": ["/usr/bin/ssh", "-N", "-T", *SSH_OPTIONS,
                             "-L", f"127.0.0.1:{local_port}:{remote_host}:{remote_port}", target],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def _bootstrap(label: str, plist: dict) -> Path:
    path = agents_dir() / f"{label}.plist"
    if path.exists():
        raise ValueError(f"LaunchAgent {path} already exists; remove it first")
    path.parent.mkdir(parents=True, exist_ok=True)
    logs_dir().mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(plist))
    try:
        subprocess.run(["launchctl", "bootstrap", _domain(), str(path)],
                       check=True, capture_output=True, timeout=30)
    except Exception:
        path.unlink(missing_ok=True)
        raise ValueError(f"launchctl could not load {label}") from None
    return path


def install_forward(sid: str, target: str, local_port: int, remote_port: int,
                    remote_host: str = "127.0.0.1") -> Path:
    if sys.platform != "darwin":
        raise ValueError("--install-forward needs macOS launchd; run the SSH forward yourself")
    return _bootstrap(forward_label(sid), forward_plist(sid, target, local_port, remote_port, remote_host))


def remove_agent(label: str) -> bool:
    """Unload and delete the LaunchAgent ``label``; False when it was absent."""
    path = agents_dir() / f"{label}.plist"
    if not path.exists():
        return False
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{label}"],
                   capture_output=True, timeout=30, check=False)
    path.unlink(missing_ok=True)
    return True


def remove_forward(sid: str) -> bool:
    return remove_agent(forward_label(sid))


def renew_plist(config: Path, restart_label: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", restart_label or ""):
        raise ValueError("Invalid runtime launchd label")
    log = str(logs_dir() / "t3-renew.log")
    return {
        "Label": RENEW_LABEL,
        "ProgramArguments": [sys.executable, "-m", "herdeck.t3_setup", "--renew-all",
                             "--config", str(config), "--restart-label", restart_label],
        # The package this command runs from, so the agent tracks this deployment.
        "EnvironmentVariables": {"PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
        "StartInterval": 3600,
        "RunAtLoad": True,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def install_renew_agent(config: Path, restart_label: str) -> Path:
    if sys.platform != "darwin":
        raise ValueError("--install-renew-agent needs macOS launchd")
    return _bootstrap(RENEW_LABEL, renew_plist(config, restart_label))
