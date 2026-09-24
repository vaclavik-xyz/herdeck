"""Bridge-side reader of ``herdeck-subagent-hook``'s per-pane spools.

The hook (``subagent_hook``) runs inside the agents, on the same host as the
bridge, and keeps ``~/.cache/herdeck/subagents/<pane>.json``. The bridge adds
the spool's entries to each wire pane as ``subagents`` (capability
"subagents") so the runtime can list them on the desktop agent card; the tile
badge still rides on the ``subagents`` metadata token.

Reads are cheap and never fatal: one ``stat`` per pane per snapshot, the file
is parsed again only when its (mtime, size, inode) changed, oversized or
corrupt files read as "no subagents" (the hook replaces spools atomically, so a
torn read cannot happen, but a hand-edited or foreign file can). The hook's
stale/drop rules are applied at read time too: a spool nobody wrote for a
while still shows a silent running subagent as stale, and never one that
should already be gone.

Event-loop only (not thread-safe), like the bridge's other per-pane caches.
"""

from __future__ import annotations

import copy
import os
import time
from collections.abc import Callable

from . import subagent_hook
from .model import parse_subagents

CAPABILITY = "subagents"
WIRE_FIELD = "subagents"
# A real spool holds <= 20 small entries (a few KiB); anything far bigger is
# not one of ours and is not worth parsing on the event loop.
MAX_FILE_BYTES = 64 * 1024
# Bound on cached panes (a herdr session never has anywhere near this many).
MAX_CACHED = 1024


class SubagentSpoolReader:
    """Per-pane spool cache; ``attach`` adds ``subagents`` to wire panes."""

    def __init__(
        self,
        directory: str | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._env = {"HERDECK_SUBAGENT_SPOOL_DIR": directory} if directory else None
        self._clock = clock
        # pane_id -> (stat key, raw spool entries as the hook wrote them)
        self._cache: dict[str, tuple[tuple[int, int, int], list[dict]]] = {}

    def _raw_entries(self, pane_id: str) -> list[dict]:
        path = subagent_hook.spool_path(pane_id, self._env)
        try:
            st = os.stat(path)
        except OSError:
            self._cache.pop(pane_id, None)
            return []
        key = (st.st_mtime_ns, st.st_size, st.st_ino)
        cached = self._cache.get(pane_id)
        if cached is not None and cached[0] == key:
            return cached[1]
        if st.st_size > MAX_FILE_BYTES:
            entries: list[dict] = []
        else:
            # load_spool never raises: a missing/corrupt/foreign file is empty.
            entries = subagent_hook.load_spool(path, pane_id)["entries"]
        if len(self._cache) >= MAX_CACHED and pane_id not in self._cache:
            self._cache.pop(next(iter(self._cache)))
        self._cache[pane_id] = (key, entries)
        return entries

    def entries_for(self, pane_id: str) -> list[dict]:
        """The pane's subagents as wire dicts, most recent first (<= 20)."""
        raw = self._raw_entries(pane_id)
        if not raw:
            return []
        # The cached entries stay as written; the time-based rules run on a copy.
        spool = {"entries": copy.deepcopy(raw)}
        subagent_hook.apply_stale_rules(spool, int(self._clock() * 1000))
        return [entry.to_wire() for entry in parse_subagents(spool["entries"])]

    def attach(self, panes: list[dict]) -> list[dict]:
        """Add ``subagents`` to each wire pane that has any (in place)."""
        seen: set[str] = set()
        for pane in panes:
            pane_id = pane.get("pane_id") if isinstance(pane, dict) else None
            if not isinstance(pane_id, str) or not pane_id:
                continue
            seen.add(pane_id)
            try:
                entries = self.entries_for(pane_id)
            except Exception:  # noqa: BLE001 - a spool must never cost a snapshot
                entries = []
            if entries:
                pane[WIRE_FIELD] = entries
            else:
                pane.pop(WIRE_FIELD, None)
        for gone in [p for p in self._cache if p not in seen]:
            del self._cache[gone]
        return panes
