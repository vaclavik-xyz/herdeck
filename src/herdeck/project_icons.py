"""Runtime-side project favicon cache, shared by every render surface.

Connectors put the bytes of each ``project_icon`` frame here, the orchestrator
(and the Elgato session) resolve a tile's icon hash through ``resolve`` —
which applies ``[view.project_icons]`` file overrides first — and every
IconProvider reads the bytes back by hash. One process-wide store
(``default_store``) keeps all of them consistent without threading a store
through every driver constructor.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .project_icon_discovery import MAX_ICON_BYTES, icon_hash, read_icon_file

log = logging.getLogger(__name__)

STORE_MAX_ENTRIES = 128
STORE_MAX_BYTES = 8 * 1024 * 1024
# How often an override file is re-stat'ed (renders run every tick).
OVERRIDE_RECHECK_S = 5.0


@dataclass(frozen=True)
class StoredIcon:
    mime: str
    data: bytes


@dataclass
class _Override:
    checked_at: float
    sig: tuple[int, int] | None  # (mtime_ns, size); None = unreadable
    hash: str | None


class ProjectIconStore:
    """Thread-safe LRU of icon bytes by content hash (connector threads write,
    render threads read) plus the override-file cache."""

    def __init__(
        self,
        *,
        max_entries: int = STORE_MAX_ENTRIES,
        max_bytes: int = STORE_MAX_BYTES,
        clock: Callable[[], float] = time.monotonic,
        override_recheck_s: float = OVERRIDE_RECHECK_S,
    ):
        self._lock = threading.Lock()
        self._icons: OrderedDict[str, StoredIcon] = OrderedDict()
        self._bytes = 0
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._clock = clock
        self._recheck = override_recheck_s
        self._overrides: dict[str, _Override] = {}
        self._warned: set[str] = set()

    def put(self, icon_hash_: str, mime: str, data: bytes) -> bool:
        """Store bytes; True only when the hash is new (worth a re-render)."""
        if not data or len(data) > MAX_ICON_BYTES:
            return False
        with self._lock:
            if icon_hash_ in self._icons:
                self._icons.move_to_end(icon_hash_)
                return False
            self._icons[icon_hash_] = StoredIcon(mime, bytes(data))
            self._bytes += len(data)
            # Known limitation: a wire icon evicted here is not re-sent until
            # the next reconnect (the bridge sends each hash once per
            # connection); its tiles show the monogram (uncached) meanwhile.
            while self._icons and (
                len(self._icons) > self._max_entries or self._bytes > self._max_bytes
            ):
                _, old = self._icons.popitem(last=False)
                self._bytes -= len(old.data)
            return icon_hash_ in self._icons

    def get(self, icon_hash_: str) -> StoredIcon | None:
        with self._lock:
            hit = self._icons.get(icon_hash_)
            if hit is not None:
                self._icons.move_to_end(icon_hash_)
            return hit

    def __contains__(self, icon_hash_: object) -> bool:
        with self._lock:
            return icon_hash_ in self._icons

    def clear(self) -> None:
        with self._lock:
            self._icons.clear()
            self._bytes = 0
            self._overrides.clear()
            self._warned.clear()

    def resolve(self, repo: str, wire_hash: str, overrides: Mapping[str, str]) -> str | None:
        """The icon hash a tile should show, or None for the monogram.

        A readable override for ``repo`` wins; otherwise the bridge's hash, but
        only once its bytes are here — a hash without bytes would pin a
        monogram in the render cache under that hash's name."""
        path = _override_path(overrides, repo) if repo else None
        if path:
            digest = self._override_hash(path)
            if digest is not None:
                return digest
        if wire_hash and wire_hash in self:
            return wire_hash
        return None

    def _override_hash(self, path: str) -> str | None:
        expanded = os.path.expanduser(path)
        now = self._clock()
        with self._lock:
            entry = self._overrides.get(expanded)
        if (
            entry is not None
            and now - entry.checked_at < self._recheck
            and (entry.hash is None or entry.hash in self)
        ):
            return entry.hash
        try:
            st = os.stat(expanded)
            sig: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        if entry is not None and sig is not None and entry.sig == sig and entry.hash in self:
            entry.checked_at = now
            return entry.hash
        digest = None
        read = read_icon_file(expanded) if sig is not None else None
        if read is not None:
            mime, data = read
            digest = icon_hash(data)
            self.put(digest, mime, data)
        with self._lock:
            self._overrides[expanded] = _Override(now, sig, digest)
            first_failure = digest is None and expanded not in self._warned
            if first_failure:
                self._warned.add(expanded)
        if first_failure:
            log.warning(
                "project icon override %s is missing, over %d bytes or not PNG/ICO/SVG; "
                "falling back to the discovered icon or monogram",
                path,
                MAX_ICON_BYTES,
            )
        return digest


def _override_path(overrides: Mapping[str, str], repo: str) -> str | None:
    """Exact key first; else a key that only differs by surrounding whitespace
    (settings keeps TOML keys verbatim, so ``" shop "`` would never match)."""
    path = overrides.get(repo)
    if path is not None:
        return path
    for key, value in overrides.items():
        if key.strip() == repo:
            return value
    return None


_DEFAULT = ProjectIconStore()


def default_store() -> ProjectIconStore:
    return _DEFAULT


def ingest_project_icon(icon, store: ProjectIconStore | None = None) -> bool:
    """Store a decoded ``protocol.ProjectIcon``; True when it was new (the
    caller re-renders so tiles waiting on it swap their monogram)."""
    target = store if store is not None else _DEFAULT
    return target.put(icon.hash, icon.mime, icon.data)


def tile_icon_fields(view, state, store: ProjectIconStore | None = None) -> dict:
    """``TileView`` kwargs (tile_icon, project_icon, project_name) for an agent
    tile. ``project_name`` is the repo name — the monogram seed and the
    ``[view.project_icons]`` key — not the (configurable) tile text line."""
    mode = getattr(view, "tile_icon", "agent")
    if mode == "agent":
        return {"tile_icon": "agent", "project_icon": None, "project_name": ""}
    name = state.repo or state.project or state.label
    target = store if store is not None else _DEFAULT
    resolved = target.resolve(
        name, getattr(state, "project_icon", ""), getattr(view, "project_icons", None) or {}
    )
    return {"tile_icon": mode, "project_icon": resolved, "project_name": name}
