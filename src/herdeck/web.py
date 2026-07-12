from __future__ import annotations

import argparse
import os
import sys

from .app import _simulator_urls, validate_web_bind
from .app import main as _app_main
from .driver.web import _default_token_path, _load_or_create_token


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="herdeck-web")
    sub = parser.add_subparsers(dest="command")
    for name in ("run", "url"):
        command = sub.add_parser(name)
        command.add_argument("--host", default=os.environ.get("HERDECK_WEB_BIND", "127.0.0.1"))
        command.add_argument(
            "--port", type=int, default=int(os.environ.get("HERDECK_WEB_PORT", "8800"))
        )
        if name == "url":
            command.add_argument("--token-file", default=_default_token_path())
    return parser


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        argv.insert(0, "run")
    args = _parser().parse_args(argv)
    host = validate_web_bind(args.host)
    if args.command == "url":
        token = _load_or_create_token(os.path.expanduser(args.token_file))
        for url in _simulator_urls(host, args.port, token):
            print(url)
        return
    os.environ["HERDECK_DECK"] = "web"
    os.environ["HERDECK_WEB_BIND"] = host
    os.environ["HERDECK_WEB_PORT"] = str(args.port)
    _app_main()


if __name__ == "__main__":
    main()
