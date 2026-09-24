"""Install / remove / inspect the usage agent from the bridge (``usage_agent``).

The bridge answers ``{"type": "usage_agent", "req", "action": "install" |
"uninstall" | "status"}`` (full token only: not in ``READONLY_MESSAGES``;
capability ``usage_agent``) by managing the usage agent service
(``herdeck-service install|uninstall usage``, usage_agent.py) for the user it
runs as:

* the agent runs the bridge's interpreter — the managed bridge venv
  (managed.py) when the bridge runs from one, so a bridge self-update restarts
  it too (:func:`restart_after_update`);
* the bridge's ``HERDECK_USAGE_CONFIG`` (its ``[usage]`` table), ``PATH`` (when
  the bridge unit set a custom one) and ``XDG_STATE_HOME`` pass through, so the
  agent polls like the bridge would and writes the file the bridge reads;
* on macOS the agent must run in the user's GUI login session (launchd
  ``gui/<uid>``): without one, install fails with ``no_gui_session``.

Reply ``data`` (the same object for every action)::

    {"action": "status" | "install" | "uninstall",
     "ok": bool,
     "code": "ok" | "no_gui_session" | "unsupported" | "failed",
     "error": str | null,            # why ok is false
     "installed": bool,              # the unit file exists
     "running": bool,                # launchd: state = running; systemd: active
     "managed": bool,                # the unit runs from the managed bridge venv
     "gui_session": bool | null,     # macOS: a GUI login session exists
     "bridge_usage": bool,           # this bridge serves usage (HERDECK_BRIDGE_USAGE=1)
     "file": str,                    # the agent's output file, as the bridge reads it
     "file_age_s": float | null,     # null = no (readable) file
     "fresh": bool,                  # the bridge uses the file's providers now
     "providers": [str, ...]}        # provider names in the file (fresh or not)

An invalid action answers ``{"type": "error", "req", "message"}`` like ``hooks``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import usage_agent

log = logging.getLogger(__name__)

CAPABILITY = "usage_agent"
ACTIONS = ("status", "install", "uninstall")
# launchctl/systemctl only (the agent runs the bridge's own interpreter, so
# nothing is downloaded); the bridge caps one request at this.
BRIDGE_TIMEOUT_S = 30.0
_COMMAND_TIMEOUT_S = 10.0
_LOCK = threading.Lock()

Run = Callable[[list[str]], tuple[int, str]]


def _run(argv: list[str]) -> tuple[int, str]:
    """(returncode, stdout+stderr); never raises for a failing command."""
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


@dataclass(frozen=True)
class Host:
    """Who and where the agent is managed for (the bridge's own user)."""

    home: Path
    uid: int
    platform: str
    env: Mapping[str, str]
    run: Run = _run


def default_host() -> Host:
    return Host(home=Path.home(), uid=os.getuid(), platform=sys.platform, env=os.environ)


def _managed_prefix() -> Path | None:
    from .self_update import probe_managed_env

    env, _reason = probe_managed_env()
    return env.prefix if env is not None else None


def _service_config(host: Host, python: str = "", extra_env=()):
    from .service import ServiceConfig

    config_path = host.env.get("HERDECK_USAGE_CONFIG")
    return ServiceConfig(
        kind="usage",
        home=host.home,
        python=python or sys.executable,
        bind="127.0.0.1",
        port=0,
        config_path=Path(os.path.expanduser(config_path)) if config_path else None,
        uid=host.uid,
        platform=host.platform,
        extra_env=tuple(extra_env),
    )


def _passthrough_env(host: Host) -> list[tuple[str, str]]:
    from .service import USAGE_SERVICE_PATH
    from .usage import bridge_usage_enabled

    out = []
    path = host.env.get("PATH")
    # A usage bridge's PATH comes from its unit: the default one, or a custom
    # --env PATH=... (e.g. an nvm node for `codex`) the agent needs as well.
    if bridge_usage_enabled(host.env.get) and path and path != USAGE_SERVICE_PATH:
        out.append(("PATH", path))
    state = host.env.get("XDG_STATE_HOME")
    if state:
        out.append(("XDG_STATE_HOME", state))
    return out


def _unit_in_prefix(program: str | None, prefix: Path | None) -> bool:
    """Does the unit's program (``<venv>/bin/python``) live in venv ``prefix``?"""
    if not program or prefix is None:
        return False
    try:
        return Path(program).parent.parent.resolve() == Path(prefix).resolve()
    except OSError:
        return False


def _gui_session(host: Host) -> bool | None:
    if host.platform != "darwin":
        return None
    code, _out = host.run(["launchctl", "print", f"gui/{host.uid}"])
    return code == 0


def _running(host: Host, config) -> bool:
    if not config.uses_launchd:
        code, _out = host.run(["systemctl", "--user", "is-active", "--quiet", config.systemd_unit])
        return code == 0
    code, out = host.run(["launchctl", "print", f"{config.launchd_domain(host.uid)}/{config.label}"])
    return code == 0 and "state = running" in out


def status(host: Host, *, managed_prefix: Path | None = None, now: float | None = None) -> dict:
    from .service import _unit_path, _unit_program
    from .usage import bridge_usage_enabled

    config = _service_config(host)
    unit = _unit_path(config)
    installed = unit.exists()
    program = _unit_program(config, unit) if installed else None
    path = usage_agent.default_path(host.env, host.home)
    doc = usage_agent.read_file(path)
    now = time.time() if now is None else now
    providers = doc.get("providers") if doc is not None else None
    return {
        "installed": installed,
        "running": installed and _running(host, config),
        "managed": _unit_in_prefix(program, managed_prefix),
        "gui_session": _gui_session(host),
        "bridge_usage": bridge_usage_enabled(host.env.get),
        "file": str(path),
        "file_age_s": usage_agent.file_age(doc, now),
        "fresh": usage_agent.is_fresh(doc, now),
        "providers": [
            p["provider"]
            for p in (providers if isinstance(providers, list) else [])
            if isinstance(p, dict) and isinstance(p.get("provider"), str)
        ][:16],
    }


class _Refused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _install(host: Host, managed_prefix: Path | None, install_service) -> None:
    from .managed import venv_python

    if getattr(sys, "frozen", False) and managed_prefix is None:
        raise _Refused(
            "unsupported",
            "this bridge runs from the desktop app, which has no Python for the usage "
            "agent; run `herdeck-service install usage --managed` on this machine",
        )
    if host.platform == "darwin" and not _gui_session(host):
        raise _Refused(
            "no_gui_session",
            f"no GUI login session for user {host.uid}: the usage agent must run in the "
            "desktop login session to reach the keychain; log in to this Mac's desktop "
            "and try again",
        )
    python = sys.executable
    if managed_prefix is not None:
        candidate = venv_python(managed_prefix)
        python = str(candidate) if candidate.exists() else python
    config = _service_config(host, python, _passthrough_env(host))
    install_service(config)


def apply(
    action: str,
    host: Host | None = None,
    *,
    managed_prefix: Callable[[], Path | None] | None = None,
    install_service=None,
    uninstall_service=None,
) -> dict:
    """Run ``action``; returns the reply ``data`` (see the module docstring)."""
    if action not in ACTIONS:
        raise ValueError(f"unknown action: {action}")
    from . import service

    host = host or default_host()
    prefix = (managed_prefix or _managed_prefix)()
    code, error = "ok", None
    with _LOCK:
        try:
            if action == "install":
                _install(
                    host,
                    prefix,
                    install_service
                    or (lambda cfg: service.install_service(cfg, runner=lambda a: host.run(a)[0])),
                )
            elif action == "uninstall":
                config = _service_config(host)
                (uninstall_service or (
                    lambda cfg: service.uninstall_service(cfg, runner=lambda a: host.run(a)[0])
                ))(config)
        except _Refused as exc:
            code, error = exc.code, exc.message
        except SystemExit as exc:  # service.py reports failures this way
            code, error = "failed", str(exc.code) if exc.code is not None else "failed"
        except (OSError, ValueError) as exc:
            code, error = "failed", str(exc)
        if error is not None:
            log.warning("usage agent %s failed: %s", action, error)
        elif action != "status":
            log.info("usage agent %s done", action)
        data = status(host, managed_prefix=prefix)
    return {"action": action, "ok": error is None, "code": code, "error": error, **data}


async def bridge_reply(msg: dict, *, timeout: float = BRIDGE_TIMEOUT_S, **kwargs) -> dict:
    """Answer ``{"type": "usage_agent", "req", "action"}`` (full token only —
    the caller enforces that). launchctl/systemctl run off the event loop."""
    req = msg.get("req")
    req = req if isinstance(req, str) else ""
    action = msg.get("action", "status")
    if action not in ACTIONS:
        return {"type": "error", "req": req, "message": "usage_agent: invalid action"}
    try:
        data = await asyncio.wait_for(asyncio.to_thread(apply, action, **kwargs), timeout)
    except TimeoutError:
        return {"type": "error", "req": req, "message": "usage_agent: timed out"}
    except Exception as exc:  # noqa: BLE001 - reported to the client, never fatal
        return {"type": "error", "req": req, "message": f"usage_agent: {exc}"}
    return {"type": "result", "req": req, "data": data}


def restart_after_update(prefix: Path, host: Host | None = None) -> bool:
    """After a bridge self-update into venv ``prefix``: restart the usage agent
    when its unit runs from that venv, so it runs the new code too. Best
    effort; True when a restart was issued and succeeded."""
    from .service import _unit_path, _unit_program

    host = host or default_host()
    config = _service_config(host)
    unit = _unit_path(config)
    if not unit.exists() or not _unit_in_prefix(_unit_program(config, unit), prefix):
        return False
    if config.uses_launchd:
        argv = ["launchctl", "kickstart", "-k", f"{config.launchd_domain(host.uid)}/{config.label}"]
    else:
        argv = ["systemctl", "--user", "restart", config.systemd_unit]
    code, out = host.run(argv)
    if code != 0:
        log.warning("could not restart the usage agent (%s): %s", " ".join(argv), out.strip())
        return False
    log.info("restarted the usage agent into the updated venv")
    return True
