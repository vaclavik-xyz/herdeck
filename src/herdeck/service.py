from __future__ import annotations

import argparse
import os
import plistlib
import pwd
import secrets
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .driver.web import normalize_web_base_path, normalize_web_origin
from .host import validate_web_bind

KINDS = ("web", "bridge", "runtime")
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


def app_runtime_binary(app: Path) -> Path:
    return app / APP_RUNTIME_BINARY


def _program_and_environment(config: ServiceConfig) -> tuple[list[str], dict[str, str]]:
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="herdeck-service")
    sub = parser.add_subparsers(dest="command", required=True)
    for action in ("install", "status", "uninstall"):
        command = sub.add_parser(action)
        command.add_argument("kind", choices=KINDS)
        command.add_argument("--home", type=Path, default=Path.home())
        command.add_argument("--uid", type=int, default=os.getuid())
        command.add_argument("--system", action="store_true")
        if action == "install":
            command.add_argument("--python", default=sys.executable)
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
                "--from-app",
                type=Path,
                nargs="?",
                const=DEFAULT_APP,
                default=None,
                metavar="APP",
                help=(
                    "runtime only (macOS): run the frozen runtime bundled in this "
                    f"desktop app (default {DEFAULT_APP}); the app's updater then "
                    "restarts the runtime together with itself"
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
    user_name = None
    if system and args.command == "install":
        user_name = pwd.getpwuid(args.uid).pw_name
    return ServiceConfig(
        kind=kind,
        home=home,
        python=getattr(args, "python", sys.executable),
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
    )


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    config = _config_from_args(args)
    if args.command == "install":
        path = install_service(config)
        print(path)
    elif args.command == "status":
        raise SystemExit(service_status(config))
    else:
        uninstall_service(config)


if __name__ == "__main__":
    main()
