"""One D200 owner per machine: an advisory exclusive lock on ``d200.lock``.

Two runtimes (the launchd ``herdeck.runtime`` and a desktop app's own spawned
sidecar) used to open the same Ulanzi D200 and fight over it — each full-frame
write overwrote the other's until one of them quit. Whoever holds this lock owns
the device; any other runtime keeps serving its window/websim over HTTP and
retries the lock, so it takes over as soon as the owner exits (the kernel drops
an ``flock`` when its process dies, even on SIGKILL).
"""

from __future__ import annotations

import os

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock; one runtime there
    fcntl = None  # type: ignore[assignment]

D200_LOCK_NAME = "d200.lock"


def d200_lock_path() -> str:
    """``$HERDECK_RUNTIME_DIR/d200.lock`` (default ``~/.cache/herdeck``), beside runtime.json."""
    base = os.environ.get("HERDECK_RUNTIME_DIR") or os.path.expanduser("~/.cache/herdeck")
    return os.path.join(base, D200_LOCK_NAME)


class DeviceLock:
    """Non-blocking exclusive ``flock``. ``acquire`` is idempotent while held."""

    def __init__(self, path: str):
        self.path = path
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        if self._fd is not None:
            return True
        if fcntl is None:  # pragma: no cover
            self._fd = -1
            return True
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        try:  # diagnostics only: who owns the deck right now
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
        except OSError:
            pass
        self._fd = fd
        return True

    def owner_pid(self) -> int | None:
        """The pid the current owner wrote, for the "owned by" warning."""
        try:
            with open(self.path, encoding="utf-8") as fh:
                return int(fh.read().strip() or 0) or None
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None or fd < 0:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
