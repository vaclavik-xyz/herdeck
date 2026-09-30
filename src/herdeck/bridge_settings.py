"""Persistent, versioned store for the bridge-owned shared settings document.

Used from the bridge event loop only (no locking). The file is TOML: metadata
keys ``revision``/``updated_at_ms``/``updated_by`` at top level, then the
shared sections. Writes are atomic (temp file + ``os.replace``) and 0600.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

from herdeck.config import ConfigError
from herdeck.shared_settings import MAX_SHARED_BYTES, parse_shared, to_raw

log = logging.getLogger(__name__)

SETTINGS_CAPABILITY = "settings"

_META = ("revision", "updated_at_ms", "updated_by")


def default_path(session: str | None = None) -> Path:
    if session is None:
        env = os.environ.get("HERDECK_BRIDGE_SETTINGS")
        if env:
            return Path(env)
        return Path.home() / ".config/herdeck/bridge-settings.toml"
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session)
    return Path.home() / f".config/herdeck/local-bridge-settings-{safe}.toml"


@dataclass
class PutResult:
    ok: bool
    revision: int
    error: str = ""
    messages: list[str] = field(default_factory=list)


class BridgeSettingsStore:
    def __init__(self, path: Path, *, clock=time.time) -> None:
        self.path = Path(path)
        self._clock = clock
        self.revision = 0
        self.updated_at_ms = 0
        self.updated_by = ""
        self.raw: dict | None = None
        self.error: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            data = tomllib.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            self._load_failed(exc)
            return
        try:
            revision = data.get("revision", 0)
            at = data.get("updated_at_ms", 0)
            by = data.get("updated_by", "")
            if (
                type(revision) is not int or revision < 1
                or type(at) is not int
                or not isinstance(by, str)
            ):
                raise ConfigError("invalid revision metadata")
            raw = to_raw(parse_shared({k: v for k, v in data.items() if k not in _META}))
        except ConfigError as exc:
            self._load_failed(exc)
            return
        self.revision, self.updated_at_ms, self.updated_by, self.raw = revision, at, by, raw

    def _load_failed(self, exc: Exception) -> None:
        self.error = f"{self.path}: {exc}"
        log.error("bridge settings unreadable, serving as unset: %s", self.error)

    def put(self, base_revision: int, raw: object, by: str) -> PutResult:
        if type(base_revision) is not int or base_revision != self.revision:
            return PutResult(False, self.revision, "stale_revision")
        try:
            size = len(json.dumps(raw, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError):
            return PutResult(False, self.revision, "invalid", ["settings is not JSON-serializable"])
        if size > MAX_SHARED_BYTES:
            return PutResult(False, self.revision, "too_large",
                             [f"settings exceed {MAX_SHARED_BYTES} bytes"])
        try:
            normalized = to_raw(parse_shared(raw))
        except ConfigError as exc:
            return PutResult(False, self.revision, "invalid", [str(exc)])
        revision = self.revision + 1
        at = int(self._clock() * 1000)
        by = str(by)
        self._write({"revision": revision, "updated_at_ms": at, "updated_by": by, **normalized})
        self.revision, self.updated_at_ms, self.updated_by = revision, at, by
        self.raw, self.error = normalized, None
        return PutResult(True, revision)

    def _write(self, doc: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            "wb", dir=self.path.parent, prefix=self.path.name + ".", delete=False
        )
        try:
            with tmp:
                tmp.write(tomli_w.dumps(doc).encode("utf-8"))
            os.chmod(tmp.name, 0o600)
            os.replace(tmp.name, self.path)
        except BaseException:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise

    def frame(self, server_id: str) -> dict:
        return {
            "type": "settings",
            "server_id": server_id,
            "revision": self.revision,
            "updated_at_ms": self.updated_at_ms,
            "updated_by": self.updated_by,
            "settings": self.raw,
        }
