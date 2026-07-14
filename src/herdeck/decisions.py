from __future__ import annotations

import hashlib
import json

from .layout import parse_options

DECISION_MAX_CHOICES = 12
DECISION_KEY_MAX_CHARS = 16
DECISION_LABEL_MAX_CHARS = 240


def decision_revision(
    server_id: str, pane_id: str, terminal_id: str, prompt: str
) -> str:
    encoded = json.dumps(
        [server_id, pane_id, terminal_id, prompt],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def decision_choices(prompt: str) -> list[dict[str, str]]:
    return [
        {
            "key": option.key,
            "label": _bounded(option.label, DECISION_LABEL_MAX_CHARS),
        }
        for option in parse_options(prompt)[:DECISION_MAX_CHOICES]
        if option.key and len(option.key) <= DECISION_KEY_MAX_CHARS and option.label
    ]


def _bounded(value: str, limit: int) -> str:
    return "".join(ch for ch in value if ch >= " " and ch != "\x7f")[:limit]
