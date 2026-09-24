"""Per-server cursor into a bridge's lifecycle events, kept across restarts.

The runtime subscribes to a bridge's events (events.py) with the last
``epoch`` + ``seq`` it saw, so a restarted runtime is replayed exactly what it
missed instead of re-alerting what it already announced. The episode ids it
has already seen ride along: after a bridge restart (new epoch) the replay
repeats whatever the bridge still knows, and those are skipped by id.

Small JSON file in the runtime cache dir (``$HERDECK_RUNTIME_DIR`` or
``~/.cache/herdeck``), 0600, atomic replace, written on every change (events
are rare). Thread-safe: each server's connector calls in on its own thread.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from collections import OrderedDict

log = logging.getLogger(__name__)

FILE_NAME = "bridge-events.json"
# Episode ids remembered per server (a fleet opens far fewer in 30 minutes).
KNOWN_MAX = 512
MAX_SERVERS = 64
MAX_FILE_BYTES = 512 * 1024


def default_path(tag: str = "") -> str:
    """``tag`` names the runtime (live_events.runtime_tag): each runtime on a
    host keeps its own cursor, so one never swallows another's alerts."""
    base = os.environ.get("HERDECK_RUNTIME_DIR") or os.path.expanduser("~/.cache/herdeck")
    name = f"bridge-events-{tag}.json" if tag else FILE_NAME
    return os.path.join(base, name)


class _Cursor:
    __slots__ = ("epoch", "seq", "known")

    def __init__(self, epoch: str | None = None, seq: int | None = None, known=()):
        self.epoch = epoch
        self.seq = seq
        self.known: OrderedDict[str, None] = OrderedDict((k, None) for k in known)


class EventCursorStore:
    def __init__(self, path: str | None):
        self._path = path
        self._lock = threading.Lock()
        self._cursors: dict[str, _Cursor] = self._load() if path else {}

    def cursor(self, server_id: str) -> tuple[str | None, int | None]:
        with self._lock:
            cur = self._cursors.get(server_id)
            return (cur.epoch, cur.seq) if cur is not None else (None, None)

    def known(self, server_id: str, episode_id: str) -> bool:
        with self._lock:
            cur = self._cursors.get(server_id)
            return cur is not None and episode_id in cur.known

    def note(
        self,
        server_id: str,
        *,
        epoch: str | None = None,
        seq: int | None = None,
        episodes=(),
    ) -> set[str]:
        """Advance ``server_id``'s cursor and remember ``episodes``; returns the
        ones not seen before. Saves when anything changed."""
        with self._lock:
            cur = self._cursors.setdefault(server_id, _Cursor())
            fresh = {e for e in episodes if e not in cur.known}
            changed = bool(fresh)
            for episode in episodes:
                cur.known[episode] = None
                cur.known.move_to_end(episode)
            while len(cur.known) > KNOWN_MAX:
                cur.known.popitem(last=False)
            if epoch is not None and (epoch, seq) != (cur.epoch, cur.seq):
                cur.epoch, cur.seq = epoch, seq
                changed = True
            if changed:
                self._save_locked()
            return fresh

    # --- persistence ---
    def _load(self) -> dict[str, _Cursor]:
        try:
            with open(self._path, "rb") as fh:
                raw = fh.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            log.warning("event cursor unreadable (%s): %s", self._path, exc)
            return {}
        try:
            data = json.loads(raw) if len(raw) <= MAX_FILE_BYTES else None
        except (ValueError, UnicodeDecodeError):
            data = None
        servers = data.get("servers") if isinstance(data, dict) and data.get("version") == 1 else None
        if not isinstance(servers, dict):
            if raw:
                log.warning("event cursor corrupt, ignored: %s", self._path)
            return {}
        out: dict[str, _Cursor] = {}
        for server_id, entry in list(servers.items())[:MAX_SERVERS]:
            if not isinstance(entry, dict):
                continue
            epoch, seq, known = entry.get("epoch"), entry.get("seq"), entry.get("known")
            if not (isinstance(epoch, str) and 0 < len(epoch) <= 64 and type(seq) is int and seq >= 0):
                epoch, seq = None, None
            ids = [k for k in known if isinstance(k, str) and len(k) <= 64][-KNOWN_MAX:] if isinstance(
                known, list
            ) else []
            out[server_id] = _Cursor(epoch, seq, ids)
        return out

    def _save_locked(self) -> None:
        if not self._path:
            return
        payload = {
            "version": 1,
            "servers": {
                sid: {"epoch": cur.epoch, "seq": cur.seq, "known": list(cur.known)}
                for sid, cur in list(self._cursors.items())[:MAX_SERVERS]
            },
        }
        directory = os.path.dirname(self._path) or "."
        tmp = None
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".bridge-events-", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                os.fchmod(fh.fileno(), 0o600)
                json.dump(payload, fh, separators=(",", ":"))
            os.replace(tmp, self._path)
            tmp = None
        except OSError as exc:
            log.warning("event cursor not saved (%s): %s", self._path, exc)
        finally:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
