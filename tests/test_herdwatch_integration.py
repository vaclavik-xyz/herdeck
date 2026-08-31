"""Cross-component contract between herdwatch metadata and Herdeck state."""

import json

import pytest

from herdeck.bridge import _herdr_pane_to_wire, _snapshot_message
from herdeck.model import Status
from herdeck.protocol import Snapshot, decode_inbound


@pytest.mark.parametrize("herdr_status", ["idle", "done", "working"])
def test_herdwatch_waiting_token_becomes_herdeck_waiting(herdr_status):
    raw_herdr_record = {
        "pane_id": "w1:p1",
        "workspace_id": "w1",
        "agent": "codex",
        "agent_status": herdr_status,
        "cwd": "/work/herdwatch",
        "tokens": {"waiting_on": "⏳ CI: test"},
    }

    wire_pane = _herdr_pane_to_wire(raw_herdr_record)
    message = decode_inbound(
        json.dumps(_snapshot_message("local", [wire_pane]))
    )

    assert isinstance(message, Snapshot)
    assert len(message.states) == 1
    state = message.states[0]
    assert state.status is Status.WAITING
    assert state.waiting_on == "⏳ CI: test"
    assert state.metadata["waiting_on"] == "⏳ CI: test"


def test_herdwatch_waiting_token_does_not_hide_a_blocked_agent():
    raw_herdr_record = {
        "pane_id": "w1:p1",
        "agent": "codex",
        "agent_status": "blocked",
        "cwd": "/work/herdwatch",
        "tokens": {"waiting_on": "⏳ CI: test"},
    }

    wire_pane = _herdr_pane_to_wire(raw_herdr_record)
    message = decode_inbound(
        json.dumps(_snapshot_message("local", [wire_pane]))
    )

    assert isinstance(message, Snapshot)
    assert message.states[0].status is Status.BLOCKED
    assert message.states[0].waiting_on == "⏳ CI: test"
