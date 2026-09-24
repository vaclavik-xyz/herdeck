"""Project marks for notification banners.

The agents run on the bridge machine, but banners are posted by the desktop
shell next to the runtime — so the favicon bytes that already travel to the
runtime for the deck tiles (``ProjectIconStore``) are written here as small
PNG files the shell can hand to macOS by path. A project without a favicon
gets the deck's monogram, so every banner carries its project's mark.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from collections.abc import Callable, Mapping

from PIL import Image

from .icons import _default_rasterize, decode_project_icon, project_mark_image
from .project_icons import ProjectIconStore, default_store

log = logging.getLogger(__name__)

# The shell only accepts banner images from a directory with this name and
# file names of [a-z0-9-] + ".png" (notifications.rs `banner_image_path`); keep in step.
DIR_NAME = "notification-icons"
# Files kept on disk; the least recently used beyond this are pruned.
MAX_FILES = 64
# Bump when the rendering changes so stale PNGs are not reused.
_VERSION = "v1"
_UNSAFE = re.compile(r"[^0-9a-z]")


def default_dir() -> str:
    base = os.environ.get("HERDECK_RUNTIME_DIR") or os.path.expanduser("~/.cache/herdeck")
    return os.path.join(base, DIR_NAME)


class NotificationIconCache:
    """``path_for(state)`` -> an absolute PNG path of the agent's project mark,
    or None when it cannot be produced (the banner then goes out without one).
    Never raises: a notification must not be lost to an icon problem."""

    def __init__(
        self,
        directory: str | None = None,
        store: ProjectIconStore | None = None,
        rasterize: Callable[[str, int], Image.Image] = _default_rasterize,
        max_files: int = MAX_FILES,
    ):
        self._dir = directory or default_dir()
        self._store = store if store is not None else default_store()
        self._rasterize = rasterize
        self._max_files = max(1, max_files)
        self._lock = threading.Lock()

    def path_for(self, state, overrides: Mapping[str, str] | None = None) -> str | None:
        name = state.repo or state.project or state.label or ""
        try:
            resolved = self._store.resolve(
                name, getattr(state, "project_icon", ""), overrides or {}
            )
            with self._lock:
                image = None
                if resolved:
                    # Only a favicon that actually decoded is filed under its
                    # hash; evicted or undecodable bytes fall through to the
                    # project's own monogram (never shared by hash).
                    path = self._path("p-" + _UNSAFE.sub("", resolved.lower()))
                    if self._reuse(path):
                        return path
                    stored = self._store.get(resolved)
                    image = self._decode(stored) if stored is not None else None
                if image is None:
                    path = self._path("m-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:16])
                    if self._reuse(path):
                        return path
                    image = project_mark_image(None, name)
                self._write(path, image)
                self._prune()
            return path
        except Exception as exc:
            log.warning("notification icon for %r unavailable: %s", name, exc)
            return None

    def _path(self, key: str) -> str:
        return os.path.join(self._dir, f"{_VERSION}-{key}.png")

    @staticmethod
    def _reuse(path: str) -> bool:
        if not os.path.exists(path):
            return False
        os.utime(path)  # LRU for _prune
        return True

    def _decode(self, stored) -> Image.Image | None:
        try:
            return decode_project_icon(stored, self._rasterize)
        except Exception as exc:
            log.debug("project icon could not be decoded, using a monogram: %s", exc)
            return None

    def _write(self, path: str, image: Image.Image) -> None:
        os.makedirs(self._dir, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        image.save(tmp, format="PNG")  # ICON_SIZE; macOS scales it down
        os.replace(tmp, path)

    def _prune(self) -> None:
        entries = []
        for entry in os.scandir(self._dir):
            if entry.name.endswith((".png", ".tmp")) and entry.is_file():
                entries.append((entry.stat().st_mtime_ns, entry.path))
        entries.sort()
        for _, path in entries[: max(0, len(entries) - self._max_files)]:
            try:
                os.remove(path)
            except OSError:
                pass
