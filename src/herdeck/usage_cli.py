from __future__ import annotations

import argparse
import sys

from .usage import capture_claude_statusline

DEFAULT_CLAUDE_CACHE = "~/.cache/herdeck/claude-usage.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="herdeck-usage")
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser(
        "capture-claude",
        help="capture Claude subscription limits from status-line JSON on stdin",
    )
    capture.add_argument("--output", default=DEFAULT_CLAUDE_CACHE)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "capture-claude":
        return 0 if capture_claude_statusline(sys.stdin.read(), args.output) else 2
    return 2
