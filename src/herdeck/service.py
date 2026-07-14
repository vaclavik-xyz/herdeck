from __future__ import annotations

import argparse
import os
import plistlib
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .app import validate_web_bind
from .driver.web import normalize_web_base_path, normalize_web_origin


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

    @property
    def label(self) -> str:
        if self.kind not in {"web", "bridge"}:
            raise ValueError("service kind must be web or bridge")
        return f"dev.herdeck.{self.kind}"


def render_launch_agent(config: ServiceConfig) -> bytes:
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
    log_path = config.home / "Library/Logs" / f"herdeck-{config.kind}.log"
    payload = {
        "Label": config.label,
        "ProgramArguments": arguments,
        "EnvironmentVariables": environment,
        "KeepAlive": True,
        "RunAtLoad": True,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    return plistlib.dumps(payload, sort_keys=True)


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


def install_service(
    config: ServiceConfig,
    *,
    runner=_run,
    token_factory=lambda: secrets.token_urlsafe(32),
) -> Path:
    validate_web_bind(config.bind)
    uid = os.getuid() if config.uid is None else config.uid
    if config.kind == "bridge":
        assert config.token_file is not None
        _ensure_private_token(config.token_file, token_factory)
    launch_agents = config.home / "Library/LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    (config.home / "Library/Logs").mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents / f"{config.label}.plist"
    existed = plist_path.exists()
    if existed:
        runner(["launchctl", "bootout", f"gui/{uid}/{config.label}"])
    plist_path.write_bytes(render_launch_agent(config))
    plist_path.chmod(0o644)
    result = runner(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)])
    if result != 0:
        raise SystemExit(f"launchctl bootstrap failed for {config.label}")
    return plist_path


def service_status(config: ServiceConfig, *, runner=_run) -> int:
    uid = os.getuid() if config.uid is None else config.uid
    return runner(["launchctl", "print", f"gui/{uid}/{config.label}"])


def uninstall_service(config: ServiceConfig, *, runner=_run) -> None:
    uid = os.getuid() if config.uid is None else config.uid
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
        command.add_argument("kind", choices=("web", "bridge"))
        command.add_argument("--home", type=Path, default=Path.home())
        command.add_argument("--uid", type=int, default=os.getuid())
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
    return parser


def _config_from_args(args) -> ServiceConfig:
    home = args.home.expanduser().resolve()
    kind = args.kind
    default_port = 8800 if kind == "web" else 8788
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
