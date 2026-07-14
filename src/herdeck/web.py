from __future__ import annotations

import argparse
import os
import sys

from .app import _simulator_urls, validate_web_bind
from .app import main as _app_main
from .driver.web import (
    _default_token_path,
    _load_or_create_token,
    normalize_web_base_path,
    normalize_web_origin,
)


def _load_persisted_token(path: str) -> str:
    token = _load_or_create_token(path)
    try:
        with open(path, encoding="utf-8") as handle:
            persisted = handle.read().strip()
    except OSError as exc:
        raise SystemExit(f"could not persist web token: {exc}") from exc
    if not persisted or persisted != token:
        raise SystemExit("could not persist web token")
    return token


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="herdeck-web")
    sub = parser.add_subparsers(dest="command")
    for name in ("run", "url"):
        command = sub.add_parser(name)
        command.add_argument("--host", default=os.environ.get("HERDECK_WEB_BIND", "127.0.0.1"))
        command.add_argument(
            "--port", type=int, default=int(os.environ.get("HERDECK_WEB_PORT", "8800"))
        )
        command.add_argument(
            "--base-path", default=os.environ.get("HERDECK_WEB_BASE_PATH", "")
        )
        command.add_argument(
            "--public-origin", default=os.environ.get("HERDECK_WEB_PUBLIC_ORIGIN", "")
        )
        command.add_argument("--frame-ancestor", action="append")
        command.add_argument(
            "--allow-query-token",
            action="store_true",
            help="enable the legacy persistent-token browser bootstrap",
        )
        if name == "url":
            command.add_argument("--token-file", default=_default_token_path())
    return parser


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        argv.insert(0, "run")
    args = _parser().parse_args(argv)
    if args.command == "url" and not args.allow_query_token:
        raise SystemExit(
            "legacy query-token URL is disabled; pass --allow-query-token to opt in"
        )
    host = validate_web_bind(args.host)
    base_path = normalize_web_base_path(args.base_path)
    public_origin = normalize_web_origin(args.public_origin)
    if args.command == "url":
        token = _load_persisted_token(os.path.expanduser(args.token_file))
        for url in _simulator_urls(
            host,
            args.port,
            token,
            base_path=base_path,
            public_origin=public_origin,
        ):
            print(url)
        return
    os.environ["HERDECK_DECK"] = "web"
    os.environ["HERDECK_WEB_BIND"] = host
    os.environ["HERDECK_WEB_PORT"] = str(args.port)
    os.environ["HERDECK_WEB_BASE_PATH"] = base_path
    os.environ["HERDECK_WEB_PUBLIC_ORIGIN"] = public_origin
    os.environ["HERDECK_WEB_ALLOW_QUERY_TOKEN"] = "1" if args.allow_query_token else "0"
    if args.frame_ancestor is not None:
        os.environ["HERDECK_WEB_FRAME_ANCESTORS"] = ",".join(args.frame_ancestor)
    _app_main()


if __name__ == "__main__":
    main()
