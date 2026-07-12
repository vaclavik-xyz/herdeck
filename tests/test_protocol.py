import pytest

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
        AgentState(AgentKey("workbox", "w1:p1"), "claude", "api", Status.BLOCKED, "api")
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


def test_decode_snapshot_preserves_terminal_identity():
    raw = (
        '{"type":"snapshot","server_id":"workbox","panes":'
        '[{"pane_id":"w1:p1","terminal_id":"term-123","agent_type":"claude",'
        '"label":"api","status":"blocked"}]}'
    )

    msg = decode_inbound(raw)

    assert msg.states[0].terminal_id == "term-123"


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


def test_decode_snapshot_preserves_workspace_and_tab():
    raw = (
        '{"type":"snapshot","server_id":"workbox","panes":'
        '[{"pane_id":"w2:p1","agent_type":"claude","label":"herdeck",'
        '"status":"working","project":"herdeck","repo":"herdeck",'
        '"branch":"main","workspace":"herdeck","tab":"2"}]}'
    )
    msg = decode_inbound(raw)
    assert msg.states[0].workspace == "herdeck"
    assert msg.states[0].tab == "2"


def test_decode_snapshot_defaults_workspace_and_tab_to_empty():
    raw = (
        '{"type":"snapshot","server_id":"workbox","panes":'
        '[{"pane_id":"w1:p1","agent_type":"claude","label":"api","status":"idle"}]}'
    )
    msg = decode_inbound(raw)
    assert msg.states[0].workspace == ""
    assert msg.states[0].tab == ""


def test_working_pane_with_custom_status_derives_waiting():
    from herdeck.model import Status
    from herdeck.protocol import _pane_to_state

    # herdwatch asserts `working` + a label while holding a pane on background
    # work; herdeck surfaces that as the distinct WAITING state.
    held = _pane_to_state("dev", {"pane_id": "p1", "status": "working", "custom_status": "⏳ ci"})
    assert held.status is Status.WAITING
    assert held.custom_status == "⏳ ci"
    # genuinely working pane (no label) stays WORKING
    plain = _pane_to_state("dev", {"pane_id": "p1", "status": "working"})
    assert plain.status is Status.WORKING and plain.custom_status == ""
    # a custom status on a non-working pane never flips the state
    idle = _pane_to_state("dev", {"pane_id": "p1", "status": "idle", "custom_status": "⏳ x"})
    assert idle.status is Status.IDLE


def test_decode_terminal_frame_preserves_wire_values():
    from herdeck.protocol import TermFrame

    msg = decode_inbound(
        '{"type":"term_frame","req":"t1","seq":3,"full":false,"cols":100,"rows":30,"data":"aGk="}'
    )
    assert msg == TermFrame("t1", 3, False, 100, 30, "aGk=")


def test_decode_terminal_closed_preserves_reason():
    from herdeck.protocol import TermClosed

    msg = decode_inbound('{"type":"term_closed","req":"t1","reason":"pane gone"}')
    assert msg == TermClosed("t1", "pane gone")


@pytest.mark.parametrize(
    "field,value",
    [
        ("seq", '"3"'),
        ("full", '"false"'),
        ("cols", "0"),
        ("rows", "-1"),
        ("data", "42"),
    ],
)
def test_decode_malformed_terminal_frame_closes_only_its_request(field, value):
    from herdeck.protocol import TermClosed

    fields = {
        "seq": "3",
        "full": "false",
        "cols": "100",
        "rows": "30",
        "data": '"aGk="',
    }
    fields[field] = value
    raw = (
        '{"type":"term_frame","req":"t1",'
        + ",".join(f'"{name}":{raw_value}' for name, raw_value in fields.items())
        + "}"
    )
    assert decode_inbound(raw) == TermClosed("t1", "invalid terminal frame", stop_remote=True)
