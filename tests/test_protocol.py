from herdeck.model import AgentKey, AgentState, Status
from herdeck.protocol import (
    Error,
    Event,
    Result,
    Snapshot,
    decode_inbound,
    encode,
)


def test_encode_is_json_line():
    line = encode({"type": "list"})
    assert line.endswith("\n")
    assert '"type": "list"' in line or '"type":"list"' in line


def test_decode_snapshot_to_states():
    raw = (
        '{"type":"snapshot","server_id":"workbox","panes":'
        '[{"pane_id":"w1:p1","agent_type":"claude","label":"api",'
        '"status":"blocked","project":"api"}]}'
    )
    msg = decode_inbound(raw)
    assert isinstance(msg, Snapshot)
    assert msg.server_id == "workbox"
    assert msg.states == [
        AgentState(AgentKey("workbox", "w1:p1"), "claude", "api",
                   Status.BLOCKED, "api")
    ]


def test_decode_snapshot_preserves_repo_and_branch():
    raw = (
        '{"type":"snapshot","server_id":"workbox","panes":'
        '[{"pane_id":"w1:p1","agent_type":"claude","label":"api",'
        '"status":"blocked","project":"api","repo":"herdeck",'
        '"branch":"feat/clawpatch"}]}'
    )
    msg = decode_inbound(raw)
    assert isinstance(msg, Snapshot)
    assert msg.states[0].repo == "herdeck"
    assert msg.states[0].branch == "feat/clawpatch"


def test_decode_event_to_state():
    raw = (
        '{"type":"event","server_id":"workbox","pane":'
        '{"pane_id":"w1:p2","agent_type":"codex","label":"web",'
        '"status":"working"}}'
    )
    msg = decode_inbound(raw)
    assert isinstance(msg, Event)
    assert msg.state.status is Status.WORKING
    assert msg.state.key == AgentKey("workbox", "w1:p2")


def test_decode_result():
    raw = '{"type":"result","req":"r1","data":{"text":"Allow edit?"}}'
    msg = decode_inbound(raw)
    assert isinstance(msg, Result)
    assert msg.req == "r1"
    assert msg.data == {"text": "Allow edit?"}


def test_unknown_status_falls_back():
    raw = (
        '{"type":"event","server_id":"x","pane":'
        '{"pane_id":"a","agent_type":"y","label":"l","status":"weird"}}'
    )
    msg = decode_inbound(raw)
    assert msg.state.status is Status.UNKNOWN


def test_decode_error():
    msg = decode_inbound('{"type":"error","message":"bad request"}')
    assert isinstance(msg, Error)
    assert msg.message == "bad request"
