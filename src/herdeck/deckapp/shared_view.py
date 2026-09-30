"""The runtime's view of each bridge's shared settings (capability ``settings``).

One entry per server id: the last ``settings`` frame the bridge sent, parsed
with the strict ``shared_settings.parse_shared``. ``LiveSource.config_for``
applies it on top of the local config, per agent's bridge.

A bridge frame always REPLACES what the runtime knew (never merges): an unset
bridge (revision 0 / no document) clears the entry and its cache file, so a
Mac that was offline cannot keep applying a stale document.

The last frame per server is cached in ``$HERDECK_RUNTIME_DIR/bridge-settings``
(or ``~/.cache/herdeck/bridge-settings``) as ``<quoted server id>.json``, 0600,
atomic replace, and loaded at start so a restarted runtime applies the right
rules before the bridge answers. Thread-safe: connectors call in on their own
threads.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from ..config import ConfigError
from ..protocol import Settings
from ..shared_settings import SharedSettings, parse_shared

log = logging.getLogger(__name__)

MAX_CACHE_BYTES = 256 * 1024
_MAX_NAME = 180


def default_cache_dir() -> Path:
    base = os.environ.get("HERDECK_RUNTIME_DIR") or os.path.expanduser("~/.cache/herdeck")
    return Path(base) / "bridge-settings"


def cache_file_name(server_id: str) -> str:
    """Deterministic, injective, path-safe file name for a server id
    (ids may contain ':' like ``local:2``; '/' and '..' can never escape)."""
    name = quote(server_id, safe="")
    if name.startswith("."):
        name = "%2E" + name[1:]
    if len(name) > _MAX_NAME:
        name = "sha256-" + hashlib.sha256(server_id.encode("utf-8")).hexdigest()
    return name + ".json"


@dataclass(frozen=True)
class _Entry:
    revision: int
    updated_at_ms: int
    updated_by: str
    raw: dict | None
    settings: SharedSettings | None
    source: str  # "bridge" | "cache" | "none"


_NONE = _Entry(0, 0, "", None, None, "none")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class SharedSettingsView:
    def __init__(self, cache_dir: Path | str | None):
        self._dir = Path(cache_dir) if cache_dir is not None else None
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = self._load() if self._dir is not None else {}

    # --- queries -------------------------------------------------------------
    def settings_for(self, server_id: str) -> SharedSettings | None:
        with self._lock:
            return self._entries.get(server_id, _NONE).settings

    def state(self, server_id: str) -> dict:
        with self._lock:
            e = self._entries.get(server_id, _NONE)
        return {
            "revision": e.revision,
            "updated_at_ms": e.updated_at_ms,
            "updated_by": e.updated_by,
            "set": e.settings is not None,
            "source": e.source if e.settings is not None else "none",
            "settings": json.loads(json.dumps(e.raw)) if e.raw is not None else None,
        }

    # --- updates -------------------------------------------------------------
    def update(self, server_id: str, frame: Settings) -> bool:
        """Apply a bridge frame. True when the effective settings (or their
        revision) changed. Invalid settings keep the previous entry."""
        revision = frame.revision if _is_int(frame.revision) else 0
        if revision <= 0 or frame.settings is None:
            entry = _Entry(
                revision=max(revision, 0),
                updated_at_ms=frame.updated_at_ms if _is_int(frame.updated_at_ms) else 0,
                updated_by=frame.updated_by if isinstance(frame.updated_by, str) else "",
                raw=None,
                settings=None,
                source="none",
            )
        else:
            try:
                parsed = parse_shared(frame.settings)
            except (ConfigError, TypeError, ValueError) as exc:
                log.warning(
                    "bridge %s sent invalid shared settings (revision %s), keeping previous: %s",
                    server_id,
                    revision,
                    exc,
                )
                return False
            entry = _Entry(
                revision=revision,
                updated_at_ms=frame.updated_at_ms if _is_int(frame.updated_at_ms) else 0,
                updated_by=frame.updated_by if isinstance(frame.updated_by, str) else "",
                raw=json.loads(json.dumps(frame.settings)),
                settings=parsed,
                source="bridge",
            )
        with self._lock:
            previous = self._entries.get(server_id, _NONE)
            self._entries[server_id] = entry
            # Cache IO under the lock: two frames for one server must land in
            # order (frames are rare).
            if entry.settings is None:
                self._remove(server_id)
            else:
                self._write(server_id, entry)
        return previous.revision != entry.revision or previous.settings != entry.settings

    def forget_live(self, server_id: str) -> None:
        """The bridge connection dropped: keep the values, mark them cached."""
        with self._lock:
            e = self._entries.get(server_id)
            if e is not None and e.source == "bridge":
                self._entries[server_id] = _Entry(
                    e.revision, e.updated_at_ms, e.updated_by, e.raw, e.settings, "cache"
                )

    # --- cache ---------------------------------------------------------------
    def _path(self, server_id: str) -> Path | None:
        return self._dir / cache_file_name(server_id) if self._dir is not None else None

    def _write(self, server_id: str, e: _Entry) -> None:
        path = self._path(server_id)
        if path is None:
            return
        doc = {
            "server_id": server_id,
            "revision": e.revision,
            "updated_at_ms": e.updated_at_ms,
            "updated_by": e.updated_by,
            "settings": e.raw,
        }
        tmp = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".bridge-settings-", suffix=".tmp")
            with os.fdopen(fd, "w") as fh:
                os.fchmod(fh.fileno(), 0o600)
                json.dump(doc, fh)
            os.replace(tmp, path)
            tmp = None
        except OSError:
            log.warning("could not write shared settings cache %s", path, exc_info=True)
        finally:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)

    def _remove(self, server_id: str) -> None:
        path = self._path(server_id)
        if path is None:
            return
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("could not remove shared settings cache %s", path, exc_info=True)

    def _load(self) -> dict[str, _Entry]:
        entries: dict[str, _Entry] = {}
        try:
            paths = sorted(self._dir.glob("*.json"))
        except OSError:
            return entries
        for path in paths:
            try:
                if path.stat().st_size > MAX_CACHE_BYTES:
                    continue
                doc = json.loads(path.read_text())
                server_id = doc["server_id"]
                revision = doc["revision"]
                if (
                    not isinstance(server_id, str)
                    or path.name != cache_file_name(server_id)
                    or not _is_int(revision)
                    or revision <= 0
                    or not isinstance(doc["settings"], dict)
                ):
                    continue
                entries[server_id] = _Entry(
                    revision=revision,
                    updated_at_ms=doc.get("updated_at_ms") if _is_int(doc.get("updated_at_ms")) else 0,
                    updated_by=doc.get("updated_by") if isinstance(doc.get("updated_by"), str) else "",
                    raw=doc["settings"],
                    settings=parse_shared(doc["settings"]),
                    source="cache",
                )
            except (OSError, ValueError, KeyError, TypeError, ConfigError):
                log.warning("ignoring unreadable shared settings cache %s", path)
        return entries
