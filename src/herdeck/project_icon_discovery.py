"""Project favicon discovery for the bridge (stdlib only).

The bridge runs on the herdr host with base dependencies only (no Pillow, no
cairosvg), so nothing here decodes an image: it finds a pane's repository
root, picks the first favicon-like file and serves its bytes by content hash.
A pane whose cwd is not inside any repo (a workspace folder grouping several
repos) falls back to that folder: its own candidates first, then those of its
direct child repos in name order (see ``find_folder_icon_file``).
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
# The non-repo folder fallback examines at most this many direct child
# directories (sorted by name, dot-dirs and symlinks skipped, non-repos counted
# too) so a pane parked in a huge folder costs a bounded number of stats.
MAX_FALLBACK_CHILDREN = 32
# ...and borrows a child repo's icon only when the folder groups at most this
# many repos (one project split into app/web/...), not a folder of projects.
MAX_FALLBACK_CHILD_REPOS = 4
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


def is_fallback_folder(path: str, *, home: str | None = None) -> bool:
    """True when ``path`` may serve as a non-repo fallback folder: an existing
    absolute directory that is neither ``$HOME``, an ancestor of it, nor the
    filesystem root (those are far too broad to stand for one project)."""
    if not path or not os.path.isabs(path):
        return False
    cur = os.path.normpath(path)
    home_dir = os.path.normpath(home if home is not None else os.path.expanduser("~"))
    if os.path.dirname(cur) == cur or cur == home_dir:
        return False
    if home_dir.startswith(cur.rstrip(os.sep) + os.sep):
        return False
    return os.path.isdir(cur)


def find_folder_icon_file(
    folder: str, *, max_children: int = MAX_FALLBACK_CHILDREN
) -> tuple[str, os.stat_result] | None:
    """Icon for a folder that is not itself a repo: the candidates in the
    folder itself, else in each direct child repo (``.git`` dir or file) in
    name order. Symlink containment is relative to the root the icon is found
    in, so a child repo cannot borrow a file from its sibling."""
    found = find_icon_file(folder)
    if found is not None:
        return found
    try:
        with os.scandir(folder) as it:
            children = sorted(
                e.name
                for e in it
                if not e.name.startswith(".")
                and not e.is_symlink()
                and e.is_dir(follow_symlinks=False)
            )
    except OSError:
        return None
    repos = [
        os.path.join(folder, name)
        for name in children[:max_children]
        if os.path.lexists(os.path.join(folder, name, ".git"))
    ]
    # A folder grouping a few repos stands for one project (app + web); a
    # folder of many independent repos (~/projects) does not, and borrowing
    # the alphabetically first repo's icon would mislabel the tile.
    if len(repos) > MAX_FALLBACK_CHILD_REPOS:
        return None
    for child in repos:
        found = find_icon_file(child)
        if found is not None:
            return found
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

    Each repo root (or non-repo fallback folder) is re-stat'ed at most every ``restat_interval`` seconds
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
        # cwd -> (checked_at, root, root is a non-repo fallback folder)
        self._roots_by_cwd: dict[str, tuple[float, str | None, bool]] = {}
        self._by_root: dict[tuple[str, bool], _RootEntry] = {}
        self._blobs: dict[str, IconBlob] = {}

    def hash_for(self, *, worktree_path: str = "", cwd: str = "") -> str:
        """The icon hash for a pane, or "" — never raises (a permissions error
        or a file racing away must not cost a snapshot)."""
        try:
            root, fallback = self._root(worktree_path, cwd)
            return self._hash_for_root(root, fallback) if root else ""
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

    def _root(self, worktree_path: str, cwd: str) -> tuple[str | None, bool]:
        if worktree_path and os.path.isdir(worktree_path):
            return os.path.normpath(worktree_path), False
        if not cwd:
            return None, False
        hit = self._roots_by_cwd.get(cwd)
        if hit is not None and self._fresh(hit[0]):
            return hit[1], hit[2]
        root = find_repo_root(cwd, home=self._home)
        fallback = False
        if root is None and is_fallback_folder(cwd, home=self._home):
            root, fallback = os.path.normpath(cwd), True
        if len(self._roots_by_cwd) >= _MAX_CACHED_PATHS:
            self._roots_by_cwd.clear()
        self._roots_by_cwd[cwd] = (self._clock(), root, fallback)
        return root, fallback

    def _hash_for_root(self, root: str, fallback: bool = False) -> str:
        key = (root, fallback)
        entry = self._by_root.get(key)
        if entry is not None and self._fresh(entry.checked_at):
            return entry.hash
        now = self._clock()
        found = find_folder_icon_file(root) if fallback else find_icon_file(root)
        if found is None:
            self._remember(key, _RootEntry(now, None, ""))
            return ""
        path, st = found
        sig = (path, st.st_mtime_ns, st.st_size)
        if entry is not None and entry.sig == sig and entry.hash in self._blobs:
            entry.checked_at = now
            return entry.hash
        read = read_icon_file(path)
        if read is None:
            self._remember(key, _RootEntry(now, None, ""))
            return ""
        mime, data = read
        digest = icon_hash(data)
        self._blobs[digest] = IconBlob(digest, mime, data)
        self._remember(key, _RootEntry(now, sig, digest))
        return digest

    def _remember(self, key: tuple[str, bool], entry: _RootEntry) -> None:
        if key not in self._by_root and len(self._by_root) >= _MAX_CACHED_PATHS:
            self._by_root.clear()
        self._by_root[key] = entry
        live = {e.hash for e in self._by_root.values() if e.hash}
        for stale in [h for h in self._blobs if h not in live]:
            del self._blobs[stale]
