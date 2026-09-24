"""Bridge-side "since when is this pane in its current status" tracking.

herdr exposes no status-change timestamp, so the runtime used to time each
status from the moment IT first saw it — every runtime restart (deploy, config
reload, app update) reset the deck's elapsed-time text to 0. The bridge is the
long-running process that sees every status change, so it stamps each wire
pane with ``status_since_ms`` (unix ms when the pane entered its current
status) and advertises the ``status_since`` capability.

The clock is keyed on ``pane_id`` + ``terminal_id``: herdr may recycle a
pane id for a new terminal, which must restart the clock. The status tracked is
the one the runtime will derive (``protocol.effective_status``), so a
herdr working/idle flip under a ``waiting_on`` token does not reset a WAITING
timer.

The table is persisted (small JSON file, 0600, atomic replace, debounced) so a
bridge restart does not reset the clocks either. A restored entry is used only
when the pane still has the same non-empty terminal identity and the same
status; anything else starts from "now".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable

from .protocol import effective_status

log = logging.getLogger(__name__)

WIRE_FIELD = "status_since_ms"
CAPABILITY = "status_since"
# Bounds: a herdr session never has anywhere near this many agent panes; the
# caps only stop a corrupt or hostile file from costing memory or time.
MAX_ENTRIES = 1024
MAX_FILE_BYTES = 256 * 1024
_MAX_ID_CHARS = 256
SAVE_DELAY_S = 2.0
# A restored timestamp further in the future than this is treated as corrupt.
_FUTURE_TOLERANCE_MS = 5_000


def default_state_path(name: str = "bridge-status-since.json") -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "herdeck", name)


class StatusSinceTracker:
    """Per-pane status start times; stamps wire panes in place.

    Event-loop only (not thread-safe), like the bridge's ProjectIconIndex."""

    def __init__(
        self,
        path: str | None = None,
        *,
        clock: Callable[[], float] = time.time,
        save_delay: float = SAVE_DELAY_S,
    ):
        self._path = path
        self._clock = clock
        self._save_delay = save_delay
        # pane_id -> (terminal_id, status value, since_ms)
        self._live: dict[str, tuple[str, str, int]] = {}
        # Entries loaded from disk, consumed (or dropped) on first sight.
        self._restored: dict[str, tuple[str, str, int]] = self._load() if path else {}
        self._dirty = False
        self._save_handle: asyncio.TimerHandle | None = None

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    # --- tracking ---
    def stamp(self, panes: list[dict]) -> list[dict]:
        """Set ``status_since_ms`` on every pane of a FULL agent snapshot.

        Panes absent from the snapshot are forgotten (a closed pane that comes
        back is a new terminal anyway)."""
        now = self._now_ms()
        live: dict[str, tuple[str, str, int]] = {}
        for pane in panes:
            pane_id = pane.get("pane_id")
            if not isinstance(pane_id, str) or not pane_id:
                continue
            terminal_id = pane.get("terminal_id") or ""
            status = effective_status(pane.get("status", "unknown"), pane.get("waiting_on")).value
            since = self._since_for(pane_id, terminal_id, status, now)
            live[pane_id] = (terminal_id, status, since)
            pane[WIRE_FIELD] = since
        if live != self._live:
            self._dirty = True
        self._live = live
        # First snapshot consumed the restore table: stale entries never linger.
        self._restored = {}
        if self._dirty:
            self._schedule_save()
        return panes

    def _since_for(self, pane_id: str, terminal_id: str, status: str, now: int) -> int:
        prev = self._live.get(pane_id)
        if prev is not None and prev[0] == terminal_id and prev[1] == status:
            return prev[2]
        if prev is None:
            restored = self._restored.get(pane_id)
            if (
                restored is not None
                and terminal_id  # identity must be provable to trust a restart
                and restored[0] == terminal_id
                and restored[1] == status
                and 0 < restored[2] <= now + _FUTURE_TOLERANCE_MS
            ):
                return min(restored[2], now)
        return now

    # --- persistence ---
    def _load(self) -> dict[str, tuple[str, str, int]]:
        try:
            with open(self._path, "rb") as fh:
                raw = fh.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            log.warning("status-since state unreadable (%s): %s", self._path, exc)
            return {}
        if len(raw) > MAX_FILE_BYTES:
            log.warning("status-since state too large, ignored: %s", self._path)
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            log.warning("status-since state corrupt, ignored: %s", self._path)
            return {}
        panes = data.get("panes") if isinstance(data, dict) and data.get("version") == 1 else None
        if not isinstance(panes, dict):
            return {}
        out: dict[str, tuple[str, str, int]] = {}
        for pane_id, entry in list(panes.items())[:MAX_ENTRIES]:
            if not isinstance(entry, dict) or len(pane_id) > _MAX_ID_CHARS:
                continue
            terminal_id, status, since = (
                entry.get("terminal_id"),
                entry.get("status"),
                entry.get("since_ms"),
            )
            if (
                isinstance(terminal_id, str)
                and len(terminal_id) <= _MAX_ID_CHARS
                and isinstance(status, str)
                and len(status) <= 32
                and type(since) is int
                and since > 0
            ):
                out[pane_id] = (terminal_id, status, since)
        return out

    def _schedule_save(self) -> None:
        if not self._path:
            self._dirty = False
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.flush()
            return
        if self._save_handle is None:
            self._save_handle = loop.call_later(self._save_delay, self.flush)

    def flush(self) -> None:
        """Write the table now if it changed (atomic replace, 0600). Never raises."""
        self._save_handle = None
        if not self._path or not self._dirty:
            return
        self._dirty = False
        entries = dict(list(self._live.items())[:MAX_ENTRIES])
        payload = {
            "version": 1,
            "panes": {
                pane_id: {"terminal_id": tid, "status": status, "since_ms": since}
                for pane_id, (tid, status, since) in entries.items()
            },
        }
        directory = os.path.dirname(self._path) or "."
        tmp = None
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".status-since-", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                os.fchmod(fh.fileno(), 0o600)
                json.dump(payload, fh, separators=(",", ":"))
            os.replace(tmp, self._path)
            tmp = None
        except OSError as exc:
            log.warning("status-since state not saved (%s): %s", self._path, exc)
        finally:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)

    def close(self) -> None:
        if self._save_handle is not None:
            self._save_handle.cancel()
        self.flush()
