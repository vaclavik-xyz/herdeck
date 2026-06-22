from __future__ import annotations

from ..model import AgentKey


class SlotLeases:
    """Sticky ordinal->agent assignment. Existing agents never move; vanished
    agents leave a hole; newcomers take the lowest free ordinal."""

    def __init__(self) -> None:
        self._lease: dict[int, AgentKey] = {}

    def update(self, ordered_present: list[AgentKey]) -> None:
        present = list(dict.fromkeys(ordered_present))  # de-dup, keep order
        present_set = set(present)
        # 1. release leases whose agent vanished (creates holes, no reflow)
        self._lease = {o: key for o, key in self._lease.items() if key in present_set}
        leased = set(self._lease.values())
        # 2. assign newcomers to the lowest free ordinal, in caller order
        for key in present:
            if key in leased:
                continue
            ordinal = 0
            while ordinal in self._lease:
                ordinal += 1
            self._lease[ordinal] = key
            leased.add(key)

    def assignment(self) -> dict[int, AgentKey]:
        return dict(self._lease)

    def ordinal_of(self, key: AgentKey) -> int | None:
        for ordinal, leased in self._lease.items():
            if leased == key:
                return ordinal
        return None
