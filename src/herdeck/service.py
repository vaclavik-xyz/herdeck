from __future__ import annotations

import argparse
import json
import os
import plistlib
import pwd
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .driver.web import normalize_web_base_path, normalize_web_origin
from .host import validate_web_bind

KINDS = ("web", "bridge", "runtime")
ACTIONS = ("install", "status", "restart", "uninstall")
# --env names that look like credentials are refused: unit files are 0644 and
# tokens belong in the keychain / token file, never in a launch environment.
_SECRET_ENV_RE = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL", re.IGNORECASE)
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DEFAULT_APP = Path("/Applications/herdeck.app")
# Where the signed desktop bundle carries its frozen converged runtime
# (desktop/herdeck-deckapp.spec -> desktop/scripts/runtime-entry.py). The desktop
# updater matches a unit's ProgramArguments against this same path to decide
# whether to restart the runtime together with the app
# (desktop/src-tauri/src/runtime_service.rs).
APP_RUNTIME_BINARY = Path("Contents/Resources/herdeck-deckapp/herdeck-deckapp")


@dataclass(frozen=True)
class ServiceConfig:
    kind: str
    home: Path
    python: str
    bind: str
    port: int
    config_path: Path | None = None
    socket_path: Path | None = None
    server_id: str = "server"
    token_file: Path | None = None
    uid: int | None = None
    base_path: str = ""
    public_origin: str = ""
    frame_ancestors: tuple[str, ...] = ()
    allow_query_token: bool = False
    system: bool = False
    system_dir: Path = Path("/Library/LaunchDaemons")
    user_name: str | None = None
    # runtime only: run the frozen runtime bundled in this desktop .app instead
    # of `python -m herdeck.runtime`.
    from_app: Path | None = None
    # "darwin" -> launchd, anything else -> systemd --user. The CLI passes
    # sys.platform; direct callers get launchd, the historical behaviour.
    platform: str = "darwin"
    # Extra non-secret launch environment (--env KEY=VALUE), e.g. the deck
    # Mac's HERDECK_D200_STANDARD_WRITER=1. Validated by validate_extra_env.
    extra_env: tuple[tuple[str, str], ...] = ()
    # bridge only: run the provider usage poller on the bridge host and push
    # usage frames (HERDECK_BRIDGE_USAGE=1; ``config_path`` becomes
    # HERDECK_USAGE_CONFIG, whose [usage] table configures the poll).
    usage: bool = False

    @property
    def label(self) -> str:
        if self.kind not in KINDS:
            raise ValueError("service kind must be web, bridge or runtime")
        return f"dev.herdeck.{self.kind}"

    @property
    def systemd_unit(self) -> str:
        if self.kind not in KINDS:
            raise ValueError("service kind must be web, bridge or runtime")
        return f"herdeck-{self.kind}.service"

    @property
    def uses_launchd(self) -> bool:
        return self.platform == "darwin"

    def launchd_domain(self, uid: int) -> str:
        """The per-user launchd domain the unit is bootstrapped into.

        The runtime drives the D200 and posts notifications, so it lives in the
        login (gui/Aqua) session, which is also where the desktop updater
        kickstarts it; the bridge and web servers are background agents."""
        return f"gui/{uid}" if self.kind == "runtime" else f"user/{uid}"


USAGE_SERVICE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def app_runtime_binary(app: Path) -> Path:
    return app / APP_RUNTIME_BINARY


def validate_extra_env(pairs, *, reserved=()) -> tuple[tuple[str, str], ...]:
    """Validate ``--env`` pairs: a plain variable name, not credential-looking,
    not one the unit already sets, no newlines."""
    out = []
    seen = set()
    for key, value in pairs:
        if not _ENV_NAME_RE.fullmatch(key):
            raise ValueError(f"--env name is not a valid variable name: {key!r}")
        if _SECRET_ENV_RE.search(key):
            raise ValueError(
                f"--env {key}: secrets never go into a service unit; store them in the "
                "keychain (the herdeck config's token_env), a [[servers]] token_file, or the "
                "bridge token file"
            )
        if key in reserved or key == "HERDECK_RUNTIME_MANAGED":
            raise ValueError(f"--env {key}: set by herdeck-service itself")
        if key in seen:
            raise ValueError(f"--env {key} given twice")
        if "\n" in value or "\r" in value or "\0" in value:
            raise ValueError(f"--env {key}: value must be a single line")
        seen.add(key)
        out.append((key, value))
    return tuple(out)


def parse_env_args(values) -> tuple[tuple[str, str], ...]:
    pairs = []
    for item in values or ():
        key, sep, value = item.partition("=")
        if not sep:
            raise ValueError(f"--env expects KEY=VALUE, got {item!r}")
        pairs.append((key, value))
    return validate_extra_env(pairs)


def _with_extra_env(
    config: ServiceConfig, arguments: list[str], environment: dict[str, str]
) -> tuple[list[str], dict[str, str]]:
    if config.extra_env:
        extra = validate_extra_env(config.extra_env, reserved=set(environment))
        environment = {**environment, **dict(extra)}
    return arguments, environment


def _program_and_environment(config: ServiceConfig) -> tuple[list[str], dict[str, str]]:
    return _with_extra_env(config, *_base_program_and_environment(config))


def _base_program_and_environment(
    config: ServiceConfig,
) -> tuple[list[str], dict[str, str]]:
    if config.from_app is not None and config.kind != "runtime":
        raise ValueError("--from-app is supported only for the runtime")
    if config.kind == "runtime":
        # The runtime mints its own per-process token and publishes it in
        # runtime.json; bridge tokens come from the config it loads. So no token
        # file of its own, and never HERDECK_RUNTIME_MANAGED (that would stop it
        # writing runtime.json, and the desktop app could not attach).
        if config.from_app is not None:
            arguments = [str(app_runtime_binary(config.from_app))]
        else:
            arguments = [config.python, "-m", "herdeck.runtime"]
        environment = {}
        if config.port:
            environment["HERDECK_DECKAPP_PORT"] = str(config.port)
        if config.config_path is not None:
            environment["HERDECK_CONFIG"] = str(config.config_path)
        return arguments, environment
    if config.kind == "bridge":
        if config.socket_path is None or config.token_file is None:
            raise ValueError("bridge service needs socket_path and token_file")
        arguments = [config.python, "-m", "herdeck.bridge"]
        environment = {
            "HERDR_SOCKET": str(config.socket_path),
            "HERDECK_BIND": config.bind,
            "HERDECK_PORT": str(config.port),
            "HERDECK_SERVER_ID": config.server_id,
            "HERDECK_TOKEN_FILE": str(config.token_file),
        }
        if config.usage:
            environment["HERDECK_BRIDGE_USAGE"] = "1"
            if config.config_path is not None:
                environment["HERDECK_USAGE_CONFIG"] = str(config.config_path)
            if not any(key == "PATH" for key, _value in config.extra_env):
                # launchd/systemd start with a minimal PATH; `codex` is a Node
                # script (#!/usr/bin/env node), so Homebrew's bin must be on it.
                # Override with --env PATH=... (e.g. for an nvm-installed node).
                environment["PATH"] = USAGE_SERVICE_PATH
    else:
        arguments = [config.python, "-m", "herdeck.web", "run"]
        if config.allow_query_token:
            arguments.append("--allow-query-token")
        base_path = normalize_web_base_path(config.base_path)
        public_origin = normalize_web_origin(config.public_origin)
        frame_ancestors = tuple(
            normalize_web_origin(origin, https_only=True)
            for origin in config.frame_ancestors
        )
        environment = {
            "HERDECK_WEB_BIND": config.bind,
            "HERDECK_WEB_PORT": str(config.port),
            "HERDECK_WEB_BASE_PATH": base_path,
            "HERDECK_WEB_PUBLIC_ORIGIN": public_origin,
            "HERDECK_WEB_FRAME_ANCESTORS": ",".join(frame_ancestors),
            "HERDECK_WEB_ALLOW_QUERY_TOKEN": "1" if config.allow_query_token else "0",
        }
        if config.config_path is not None:
            environment["HERDECK_CONFIG"] = str(config.config_path)
    return arguments, environment


def render_launch_agent(config: ServiceConfig) -> bytes:
    arguments, environment = _program_and_environment(config)
    log_path = config.home / "Library/Logs" / f"herdeck-{config.kind}.log"
    payload = {
        "Label": config.label,
        "ProgramArguments": arguments,
        "EnvironmentVariables": environment,
        "KeepAlive": True,
        "LimitLoadToSessionType": "Aqua" if config.kind == "runtime" else "Background",
        "RunAtLoad": True,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    if config.system:
        if config.user_name is None:
            raise ValueError("system service needs a target user name")
        payload.pop("LimitLoadToSessionType")
        payload["UserName"] = config.user_name
        payload["ProcessType"] = "Background"
        payload["ThrottleInterval"] = 10
    return plistlib.dumps(payload, sort_keys=True)


_SYSTEMD_PLAIN = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/._-=:,+@")

_SYSTEMD_DESCRIPTIONS = {
    "bridge": "Herdeck bridge (herdr socket -> WebSocket)",
    "runtime": "Herdeck deck runtime (D200 + desktop window API)",
    "web": "Herdeck web deck",
}


def _systemd_quote(value: str, *, always: bool = False) -> str:
    """One systemd word: `%` is a specifier (and `$` expands in ExecStart, which
    is the only place unquoted words go); quote anything not plainly safe."""
    if "\n" in value:
        raise ValueError("systemd unit values cannot contain newlines")
    escaped = value.replace("%", "%%")
    if not always:
        escaped = escaped.replace("$", "$$")
    if not always and escaped and set(escaped) <= _SYSTEMD_PLAIN | {"%"}:
        return escaped
    return '"' + escaped.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_systemd_unit(config: ServiceConfig) -> str:
    """A systemd --user unit. Logs go to the journal: journalctl --user -u <unit>."""
    arguments, environment = _program_and_environment(config)
    lines = [
        "[Unit]",
        f"Description={_SYSTEMD_DESCRIPTIONS[config.kind]}",
        "After=network-online.target",
        "",
        "[Service]",
    ]
    lines += [
        "Environment=" + _systemd_quote(f"{key}={value}", always=True)
        for key, value in environment.items()
    ]
    lines += [
        "ExecStart=" + " ".join(_systemd_quote(argument) for argument in arguments),
        "Restart=always",
        "RestartSec=2",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def _run(command: list[str]) -> int:
    return subprocess.run(command, check=False).returncode


def _ensure_private_token(path: Path, token_factory) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        if path.stat().st_mode & 0o077:
            raise SystemExit(f"token file permissions must be 0600 or stricter: {path}")
        if not path.read_text(encoding="utf-8").strip():
            raise SystemExit(f"token file is empty: {path}")
        return
    token = token_factory()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)


def _validate_from_app(config: ServiceConfig) -> None:
    if config.kind != "runtime":
        raise ValueError("--from-app is supported only for the runtime")
    if not config.uses_launchd:
        raise SystemExit("--from-app needs macOS: the desktop .app bundle exists only there")
    binary = app_runtime_binary(config.from_app)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise SystemExit(
            f"no bundled runtime in {config.from_app}: expected an executable {binary}"
        )


def _systemd_user_dir(config: ServiceConfig) -> Path:
    return config.home / ".config/systemd/user"


def _install_systemd(config: ServiceConfig, *, runner, token_factory) -> Path:
    if config.kind == "bridge":
        assert config.token_file is not None
        _ensure_private_token(config.token_file, token_factory)
    unit_dir = _systemd_user_dir(config)
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / config.systemd_unit
    unit_path.write_text(render_systemd_unit(config), encoding="utf-8")
    unit_path.chmod(0o644)
    for command in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", config.systemd_unit],
        # restart, not start: a reinstall must pick up the new unit.
        ["systemctl", "--user", "restart", config.systemd_unit],
    ):
        if runner(command) != 0:
            raise SystemExit(f"{' '.join(command)} failed for {config.systemd_unit}")
    return unit_path


def install_service(
    config: ServiceConfig,
    *,
    runner=_run,
    token_factory=lambda: secrets.token_urlsafe(32),
) -> Path:
    validate_web_bind(config.bind)
    if config.system and config.kind != "bridge":
        raise ValueError("system services are supported only for the bridge")
    if config.from_app is not None:
        _validate_from_app(config)
    if not config.uses_launchd:
        if config.system:
            raise SystemExit("--system is a launchd (macOS) option; systemd units are --user")
        return _install_systemd(config, runner=runner, token_factory=token_factory)
    uid = os.getuid() if config.uid is None else config.uid
    if config.kind == "bridge":
        assert config.token_file is not None
        _ensure_private_token(config.token_file, token_factory)
    launch_agents = config.home / "Library/LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    (config.home / "Library/Logs").mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents / f"{config.label}.plist"
    existed = plist_path.exists()
    legacy_domain = None
    if existed and config.system:
        for domain in (f"user/{uid}", f"gui/{uid}"):
            if runner(["launchctl", "print", f"{domain}/{config.label}"]) == 0:
                legacy_domain = domain
                break
    if existed:
        runner(["launchctl", "bootout", f"gui/{uid}/{config.label}"])
        runner(["launchctl", "bootout", f"user/{uid}/{config.label}"])
    if config.system:
        system_path = config.system_dir / f"{config.label}.plist"
        previous_path = None
        if system_path.exists():
            with tempfile.NamedTemporaryFile(
                prefix=f"{config.label}.previous.", suffix=".plist", delete=False
            ) as previous:
                previous_path = Path(previous.name)
                previous.write(system_path.read_bytes())
        runner(["sudo", "launchctl", "bootout", f"system/{config.label}"])
        with tempfile.NamedTemporaryFile(prefix=f"{config.label}.", suffix=".plist", delete=False) as handle:
            temporary_path = Path(handle.name)
            handle.write(render_launch_agent(config))
        try:
            install_result = runner(
                [
                    "sudo",
                    "/usr/bin/install",
                    "-o",
                    "root",
                    "-g",
                    "wheel",
                    "-m",
                    "0644",
                    str(temporary_path),
                    str(system_path),
                ]
            )
            if install_result != 0:
                raise SystemExit(f"could not install system service {config.label}")
            bootstrap_result = runner(
                ["sudo", "launchctl", "bootstrap", "system", str(system_path)]
            )
            if bootstrap_result != 0:
                raise SystemExit(f"launchctl bootstrap failed for {config.label}")
            if runner(["launchctl", "print", f"system/{config.label}"]) != 0:
                raise SystemExit(f"launchctl could not verify system service {config.label}")
        except SystemExit as error:
            runner(["sudo", "launchctl", "bootout", f"system/{config.label}"])
            if previous_path is not None:
                restore_result = runner(
                    [
                        "sudo",
                        "/usr/bin/install",
                        "-o",
                        "root",
                        "-g",
                        "wheel",
                        "-m",
                        "0644",
                        str(previous_path),
                        str(system_path),
                    ]
                )
                restart_result = runner(
                    ["sudo", "launchctl", "bootstrap", "system", str(system_path)]
                )
                if restore_result != 0 or restart_result != 0:
                    raise SystemExit(
                        f"system service update and rollback failed for {config.label}"
                    ) from error
            else:
                runner(["sudo", "/bin/rm", "-f", str(system_path)])
                if legacy_domain is not None:
                    restore_result = runner(
                        ["launchctl", "bootstrap", legacy_domain, str(plist_path)]
                    )
                    if restore_result != 0:
                        raise SystemExit(
                            f"system service migration and rollback failed for {config.label}"
                        ) from error
            raise
        finally:
            temporary_path.unlink(missing_ok=True)
            if previous_path is not None:
                previous_path.unlink(missing_ok=True)
        if existed:
            plist_path.unlink()
        return system_path
    plist_path.write_bytes(render_launch_agent(config))
    plist_path.chmod(0o644)
    result = runner(["launchctl", "bootstrap", config.launchd_domain(uid), str(plist_path)])
    if result != 0:
        raise SystemExit(f"launchctl bootstrap failed for {config.label}")
    return plist_path


def _unit_path(config: ServiceConfig) -> Path:
    if not config.uses_launchd:
        return _systemd_user_dir(config) / config.systemd_unit
    if config.system:
        return config.system_dir / f"{config.label}.plist"
    return config.home / "Library/LaunchAgents" / f"{config.label}.plist"


def _unit_program(config: ServiceConfig, path: Path) -> str | None:
    """The executable the installed unit runs (first ProgramArguments / ExecStart word)."""
    try:
        if config.uses_launchd:
            arguments = plistlib.loads(path.read_bytes()).get("ProgramArguments") or []
            return arguments[0] if arguments and isinstance(arguments[0], str) else None
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("ExecStart="):
                words = shlex.split(line[len("ExecStart="):])
                return words[0] if words else None
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return None


def _quiet_run(command: list[str]) -> int:
    return subprocess.run(
        command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode


def service_status_info(config: ServiceConfig, *, runner=None) -> dict:
    """Machine-readable status (``status --json``) for the desktop app."""
    runner = runner or _quiet_run
    path = _unit_path(config)
    installed = path.exists()
    program = _unit_program(config, path) if installed else None
    if not config.uses_launchd:
        probe = ["systemctl", "--user", "is-active", "--quiet", config.systemd_unit]
    elif config.system:
        probe = ["launchctl", "print", f"system/{config.label}"]
    else:
        uid = os.getuid() if config.uid is None else config.uid
        probe = ["launchctl", "print", f"{config.launchd_domain(uid)}/{config.label}"]
    return {
        "kind": config.kind,
        "label": config.label if config.uses_launchd else config.systemd_unit,
        "installed": installed,
        "unit_path": str(path),
        "loaded": runner(probe) == 0,
        "program": program,
        "from_app": bool(program) and program.endswith("/" + str(APP_RUNTIME_BINARY)),
    }


def restart_service(config: ServiceConfig, *, runner=None) -> int:
    """Restart the installed service in place (``launchctl kickstart -k`` /
    ``systemctl --user restart``); the unit must already be loaded."""
    runner = runner or _run
    if not config.uses_launchd:
        return runner(["systemctl", "--user", "restart", config.systemd_unit])
    if config.system:
        return runner(["sudo", "launchctl", "kickstart", "-k", f"system/{config.label}"])
    uid = os.getuid() if config.uid is None else config.uid
    return runner(["launchctl", "kickstart", "-k", f"{config.launchd_domain(uid)}/{config.label}"])


def service_status(config: ServiceConfig, *, runner=_run) -> int:
    if not config.uses_launchd:
        return runner(["systemctl", "--user", "status", "--no-pager", config.systemd_unit])
    if config.system:
        return runner(["launchctl", "print", f"system/{config.label}"])
    uid = os.getuid() if config.uid is None else config.uid
    return runner(["launchctl", "print", f"{config.launchd_domain(uid)}/{config.label}"])


def uninstall_service(config: ServiceConfig, *, runner=_run) -> None:
    if not config.uses_launchd:
        runner(["systemctl", "--user", "disable", "--now", config.systemd_unit])
        (_systemd_user_dir(config) / config.systemd_unit).unlink(missing_ok=True)
        runner(["systemctl", "--user", "daemon-reload"])
        return
    if config.system:
        runner(["sudo", "launchctl", "bootout", f"system/{config.label}"])
        system_path = config.system_dir / f"{config.label}.plist"
        runner(["sudo", "/bin/rm", "-f", str(system_path)])
        return
    uid = os.getuid() if config.uid is None else config.uid
    runner(["launchctl", "bootout", f"user/{uid}/{config.label}"])
    runner(["launchctl", "bootout", f"gui/{uid}/{config.label}"])
    plist_path = config.home / "Library/LaunchAgents" / f"{config.label}.plist"
    try:
        plist_path.unlink()
    except FileNotFoundError:
        pass


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _default_from_app() -> Path:
    """``--from-app`` without a path: run from the desktop app's bundled
    binary means "this app"; otherwise the standard install location."""
    if _frozen():
        executable = Path(sys.executable).resolve()
        depth = len(APP_RUNTIME_BINARY.parts)
        if executable.parts[-depth:] == APP_RUNTIME_BINARY.parts:
            return executable.parents[depth - 1]
    return DEFAULT_APP


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="herdeck-service")
    sub = parser.add_subparsers(dest="command", required=True)
    for action in ACTIONS:
        command = sub.add_parser(action)
        command.add_argument("kind", choices=KINDS)
        command.add_argument("--home", type=Path, default=Path.home())
        command.add_argument("--uid", type=int, default=os.getuid())
        command.add_argument("--system", action="store_true")
        if action == "status":
            command.add_argument(
                "--json", action="store_true", help="print a machine-readable status object"
            )
        if action == "install":
            command.add_argument("--python", default=None)
            command.add_argument("--bind", default="127.0.0.1")
            command.add_argument("--port", type=int)
            command.add_argument("--config", type=Path)
            command.add_argument("--socket", type=Path)
            command.add_argument("--server-id", default="server")
            command.add_argument("--token-file", type=Path)
            command.add_argument("--base-path", default="")
            command.add_argument("--public-origin", default="")
            command.add_argument("--frame-ancestor", action="append", default=[])
            command.add_argument("--allow-query-token", action="store_true")
            command.add_argument(
                "--env",
                action="append",
                default=[],
                metavar="KEY=VALUE",
                help=(
                    "extra launch environment (repeatable, non-secret only: names with "
                    "TOKEN/SECRET/PASSWORD are refused)"
                ),
            )
            command.add_argument(
                "--usage",
                action="store_true",
                help=(
                    "bridge only: poll Codex/Claude usage on this host and push it to "
                    "every runtime (sets HERDECK_BRIDGE_USAGE=1; --config PATH becomes "
                    "HERDECK_USAGE_CONFIG, whose [usage] table configures the poll)"
                ),
            )
            command.add_argument(
                "--managed",
                action="store_true",
                help=(
                    "bridge only: install the herdeck release into "
                    "~/.local/share/herdeck/bridge-venv and run the bridge from it"
                ),
            )
            command.add_argument(
                "--version",
                dest="managed_version",
                default=None,
                metavar="X",
                help="with --managed: the herdeck release to install (default: this version)",
            )
            command.add_argument(
                "--from-app",
                type=Path,
                nargs="?",
                const=_default_from_app(),
                default=None,
                metavar="APP",
                help=(
                    "runtime only (macOS): run the frozen runtime bundled in this "
                    f"desktop app (default {DEFAULT_APP}, or this app when run from "
                    "its bundle); the app's updater then restarts the runtime "
                    "together with itself"
                ),
            )
    return parser


def _config_from_args(args) -> ServiceConfig:
    home = args.home.expanduser().resolve()
    kind = args.kind
    # The runtime picks a free loopback port and publishes it in runtime.json
    # unless --port pins one.
    default_port = {"web": 8800, "bridge": 8788, "runtime": 0}[kind]
    system = getattr(args, "system", False)
    if system and kind != "bridge":
        raise SystemExit("--system is supported only for the bridge")
    from_app = getattr(args, "from_app", None)
    if from_app is not None:
        if kind != "runtime":
            raise SystemExit("--from-app is supported only for the runtime")
        # Resolve here: the desktop updater compares the unit's program path
        # against its own (canonical) bundle path.
        from_app = from_app.expanduser().resolve()
    usage = getattr(args, "usage", False)
    if usage and kind != "bridge":
        raise SystemExit("--usage is supported only for the bridge")
    managed = getattr(args, "managed", False)
    if managed and kind != "bridge":
        raise SystemExit("--managed is supported only for the bridge")
    if getattr(args, "managed_version", None) is not None and not managed:
        raise SystemExit("--version needs --managed")
    python = getattr(args, "python", None)
    if managed and python is not None:
        raise SystemExit("--managed runs the bridge from its own venv; drop --python")
    if python is None:
        python = sys.executable
        if (
            args.command == "install"
            and _frozen()
            and not managed
            and not (kind == "runtime" and from_app is not None)
        ):
            # The app's frozen binary is not a Python interpreter.
            raise SystemExit(
                "run from the desktop app, install supports only `runtime --from-app` "
                "and `bridge --managed`; pass --python for anything else"
            )
    try:
        extra_env = parse_env_args(getattr(args, "env", ()))
    except ValueError as error:
        raise SystemExit(str(error)) from None
    user_name = None
    if system and args.command == "install":
        user_name = pwd.getpwuid(args.uid).pw_name
    return ServiceConfig(
        kind=kind,
        home=home,
        python=python,
        bind=getattr(args, "bind", "127.0.0.1"),
        port=getattr(args, "port", None) or default_port,
        config_path=getattr(args, "config", None),
        socket_path=getattr(args, "socket", None) or home / ".config/herdr/herdr.sock",
        server_id=getattr(args, "server_id", "server"),
        token_file=getattr(args, "token_file", None) or home / ".config/herdeck/bridge-token",
        uid=args.uid,
        base_path=getattr(args, "base_path", ""),
        public_origin=getattr(args, "public_origin", ""),
        frame_ancestors=tuple(getattr(args, "frame_ancestor", ())),
        allow_query_token=getattr(args, "allow_query_token", False),
        system=system,
        user_name=user_name,
        from_app=from_app,
        platform=sys.platform,
        extra_env=extra_env,
        usage=usage,
    )


def install_managed_bridge(
    config: ServiceConfig,
    version: str | None = None,
    *,
    installer=None,
    runner=_run,
    token_factory=lambda: secrets.token_urlsafe(32),
) -> Path:
    """Install the herdeck release into the managed bridge venv, then the
    bridge service running that venv's interpreter."""
    from dataclasses import replace

    from . import __version__
    from .managed import ManagedInstaller, ManagedInstallError, managed_venv_dir, venv_python

    if config.kind != "bridge":
        raise ValueError("--managed is supported only for the bridge")
    venv = managed_venv_dir(config.home)
    installer = installer or ManagedInstaller()
    try:
        installer.install(venv, version or __version__)
    except ManagedInstallError as error:
        raise SystemExit(f"managed bridge install failed: {error}") from None
    return install_service(
        replace(config, python=str(venv_python(venv))),
        runner=runner,
        token_factory=token_factory,
    )


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    config = _config_from_args(args)
    if args.command == "install":
        if getattr(args, "managed", False):
            path = install_managed_bridge(config, args.managed_version)
        else:
            path = install_service(config)
        print(path)
    elif args.command == "status":
        if args.json:
            print(json.dumps(service_status_info(config)))
            return
        raise SystemExit(service_status(config))
    elif args.command == "restart":
        raise SystemExit(restart_service(config))
    else:
        uninstall_service(config)


if __name__ == "__main__":
    main()
