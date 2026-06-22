from __future__ import annotations

import json

PROTOCOL_VERSION = 1


class ProtocolError(Exception):
    pass


def encode(msg: dict) -> bytes:
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode()


def decode(line: bytes | str) -> dict:
    if isinstance(line, bytes):
        line = line.decode()
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(str(exc)) from exc
    if not isinstance(value, dict):
        raise ProtocolError("message must be a JSON object")
    return value
