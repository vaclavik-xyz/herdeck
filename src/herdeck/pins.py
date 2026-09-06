"""Local deck pin state, separate from shared configuration and keyed by profile."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .model import AgentKey


class PinStore:
    def __init__(self, path: Path):
        self.path = path

    def _read(self):
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text())
        if not isinstance(data, dict) or any(
            not isinstance(entries, list) for entries in data.values()
        ):
            raise ValueError("Invalid pin store")
        return data

    def load(self, profile: str) -> dict[int, AgentKey]:
        pins = {}
        seen = set()
        for entry in self._read().get(profile, []):
            if not isinstance(entry, dict) or not all(
                isinstance(entry.get(k), str) and entry[k] for k in ("server", "agent")
            ):
                raise ValueError("Invalid pin identity")
            position = entry["position"]
            key = AgentKey(entry["server"], entry["agent"])
            if type(position) is not int or not 0 <= position < 1024 or key in seen:
                raise ValueError("Invalid or duplicate deck pin")
            if position in pins:
                raise ValueError("Duplicate pin position")
            pins[position] = key
            seen.add(key)
        return pins

    def save(self, profile: str, pins: dict[int, AgentKey]) -> None:
        data = self._read()
        data[profile] = [
            dict(position=i, server=key.server_id, agent=key.pane_id)
            for i, key in sorted(pins.items())
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix=".pins-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
