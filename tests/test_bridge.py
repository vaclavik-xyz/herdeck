import json

import pytest

from herdeck.bridge import handle_client_message, StubHerdr


@pytest.fixture
def herdr():
    return StubHerdr(panes=[
        {"pane_id": "w1:p1", "agent_type": "claude", "label": "api",
         "status": "blocked", "project": "api"},
    ])


async def test_list_returns_snapshot(herdr):
    out = await handle_client_message(herdr, "workbox", '{"type":"list"}')
    msg = json.loads(out)
    assert msg["type"] == "snapshot"
    assert msg["server_id"] == "workbox"
    assert msg["panes"][0]["pane_id"] == "w1:p1"


async def test_read_returns_result(herdr):
    herdr.detection["w1:p1"] = "Allow edit to config.py?"
    out = await handle_client_message(
        herdr, "workbox",
        '{"type":"read","req":"r1","pane_id":"w1:p1","source":"detection"}')
    msg = json.loads(out)
    assert msg["type"] == "result"
    assert msg["req"] == "r1"
    assert msg["data"]["text"] == "Allow edit to config.py?"


async def test_act_if_blocked_sends_keys_when_blocked(herdr):
    out = await handle_client_message(
        herdr, "workbox",
        '{"type":"act","req":"r2","pane_id":"w1:p1","keys":["1","enter"]}')
    msg = json.loads(out)
    assert msg["type"] == "result"
    assert herdr.sent == [("w1:p1", ["1", "enter"])]


async def test_act_skipped_when_not_blocked(herdr):
    herdr.panes[0]["status"] = "working"
    out = await handle_client_message(
        herdr, "workbox",
        '{"type":"act","req":"r3","pane_id":"w1:p1","keys":["1"]}')
    msg = json.loads(out)
    assert msg["data"]["skipped"] is True
    assert herdr.sent == []
