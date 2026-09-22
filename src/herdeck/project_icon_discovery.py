"""Project favicon discovery for the bridge (stdlib only).

The bridge runs on the herdr host with base dependencies only (no Pillow, no
cairosvg), so nothing here decodes an image: it finds a pane's repository
root, picks the first favicon-like file and serves its bytes by content hash.
The runtime decodes (icons.py). Hashing and the size/format limits live here
so the runtime's [view.project_icons] overrides hash exactly like the bridge.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

MAX_ICON_BYTES = 256 * 1024
MAX_WALK_LEVELS = 12
RESTAT_INTERVAL_S = 60.0
# Raster first: the frozen app and the Elgato plugin cannot rasterise SVG.
CANDIDATES: tuple[str, ...] = (
    "favicon.png",
    "public/favicon.png",
    "static/favicon.png",
    "assets/favicon.png",
    "app/icon.png",
    "src/app/icon.png",
    "public/apple-touch-icon.png",
    "apple-touch-icon.png",
    "public/icon.png",
    "icon.png",
    "favicon.ico",
    "public/favicon.ico",
    "static/favicon.ico",
    "app/favicon.ico",
    "src/app/favicon.ico",
    "favicon.svg",
    "public/favicon.svg",
    "static/favicon.svg",
    "app/icon.svg",
    "src/app/icon.svg",
    "assets/logo.svg",
)
MIME_BY_EXT = {".png": "image/png", ".ico": "image/x-icon", ".svg": "image/svg+xml"}
ICON_MIMES = frozenset(MIME_BY_EXT.values())
# Bounds the per-cwd / per-root caches (a long-running bridge sees a handful).
_MAX_CACHED_PATHS = 1024


def icon_hash(data: bytes) -> str:
    """Content hash used on the wire and as every cache key."""
    return hashlib.sha256(data).hexdigest()[:16]


def mime_for_path(path: str) -> str | None:
    return MIME_BY_EXT.get(os.path.splitext(path)[1].lower())


def read_icon_file(path: str) -> tuple[str, bytes] | None:
    """(mime, bytes) of a regular icon file of 1..MAX_ICON_BYTES bytes with a
    known extension, else None. Stat first: opening a FIFO would block."""
    mime = mime_for_path(path)
    if mime is None:
        return None
    try:
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode) or not 1 <= st.st_size <= MAX_ICON_BYTES:
            return None
        with open(path, "rb") as fh:
            data = fh.read(MAX_ICON_BYTES + 1)
    except OSError:
        return None
    if not 1 <= len(data) <= MAX_ICON_BYTES:
        return None
    return mime, data


def find_repo_root(
    start: str, *, home: str | None = None, max_levels: int = MAX_WALK_LEVELS
) -> str | None:
    """Nearest ancestor of ``start`` (inclusive) holding a ``.git`` dir or file.

    Checks at most ``max_levels`` directories and never looks at ``$HOME``'s
    parent or the filesystem root, so a stray ``/Users/.git`` can never make
    every pane one project."""
    if not start or not os.path.isabs(start):
        return None
    home_dir = os.path.normpath(home if home is not None else os.path.expanduser("~"))
    stop = os.path.dirname(home_dir)
    cur = os.path.normpath(start)
    for _ in range(max_levels):
        parent = os.path.dirname(cur)
        if cur == stop or parent == cur:
            return None
        if os.path.lexists(os.path.join(cur, ".git")):
            return cur
        cur = parent
    return None


def find_icon_file(root: str) -> tuple[str, os.stat_result] | None:
    """First candidate under ``root`` that is a regular file of 1..MAX bytes,
    with symlinks resolved but required to stay inside the repo."""
    real_root = os.path.realpath(root)
    for rel in CANDIDATES:
        try:
            real = os.path.realpath(os.path.join(root, rel))
            if os.path.commonpath([real, real_root]) != real_root:
                continue
            st = os.stat(real)
        except (OSError, ValueError):
            continue
        if stat.S_ISREG(st.st_mode) and 1 <= st.st_size <= MAX_ICON_BYTES:
            return real, st
    return None


@dataclass(frozen=True)
class IconBlob:
    hash: str
    mime: str
    data: bytes


@dataclass
class _RootEntry:
    checked_at: float
    sig: tuple[str, int, int] | None  # (resolved path, mtime_ns, size)
    hash: str


class ProjectIconIndex:
    """Bridge-side cache: pane location -> icon hash, hash -> bytes.

    Each repo root is re-stat'ed at most every ``restat_interval`` seconds
    (``invalidate`` forces it, e.g. on a herdr worktree event); the file is
    re-read only when its (path, mtime_ns, size) changed. Blobs no repo
    references any more are dropped."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        home: str | None = None,
        restat_interval: float = RESTAT_INTERVAL_S,
    ):
        self._clock = clock
        self._home = home
        self._interval = restat_interval
        self._roots_by_cwd: dict[str, tuple[float, str | None]] = {}
        self._by_root: dict[str, _RootEntry] = {}
        self._blobs: dict[str, IconBlob] = {}

    def hash_for(self, *, worktree_path: str = "", cwd: str = "") -> str:
        """The icon hash for a pane, or "" — never raises (a permissions error
        or a file racing away must not cost a snapshot)."""
        try:
            root = self._root(worktree_path, cwd)
            return self._hash_for_root(root) if root else ""
        except Exception as exc:
            log.debug("project icon discovery failed for %s: %s", worktree_path or cwd, exc)
            return ""

    def blob(self, icon_hash: str) -> IconBlob | None:
        return self._blobs.get(icon_hash)

    def invalidate(self) -> None:
        self._roots_by_cwd.clear()
        for entry in self._by_root.values():
            entry.checked_at = float("-inf")

    def _fresh(self, checked_at: float) -> bool:
        return self._clock() - checked_at < self._interval

    def _root(self, worktree_path: str, cwd: str) -> str | None:
        if worktree_path and os.path.isdir(worktree_path):
            return os.path.normpath(worktree_path)
        if not cwd:
            return None
        hit = self._roots_by_cwd.get(cwd)
        if hit is not None and self._fresh(hit[0]):
            return hit[1]
        root = find_repo_root(cwd, home=self._home)
        if len(self._roots_by_cwd) >= _MAX_CACHED_PATHS:
            self._roots_by_cwd.clear()
        self._roots_by_cwd[cwd] = (self._clock(), root)
        return root

    def _hash_for_root(self, root: str) -> str:
        entry = self._by_root.get(root)
        if entry is not None and self._fresh(entry.checked_at):
            return entry.hash
        now = self._clock()
        found = find_icon_file(root)
        if found is None:
            self._remember(root, _RootEntry(now, None, ""))
            return ""
        path, st = found
        sig = (path, st.st_mtime_ns, st.st_size)
        if entry is not None and entry.sig == sig and entry.hash in self._blobs:
            entry.checked_at = now
            return entry.hash
        read = read_icon_file(path)
        if read is None:
            self._remember(root, _RootEntry(now, None, ""))
            return ""
        mime, data = read
        digest = icon_hash(data)
        self._blobs[digest] = IconBlob(digest, mime, data)
        self._remember(root, _RootEntry(now, sig, digest))
        return digest

    def _remember(self, root: str, entry: _RootEntry) -> None:
        if root not in self._by_root and len(self._by_root) >= _MAX_CACHED_PATHS:
            self._by_root.clear()
        self._by_root[root] = entry
        live = {e.hash for e in self._by_root.values() if e.hash}
        for stale in [h for h in self._blobs if h not in live]:
            del self._blobs[stale]
