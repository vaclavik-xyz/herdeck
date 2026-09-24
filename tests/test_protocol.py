import base64
import json

import pytest

from herdeck.model import AgentKey, AgentState, Status, WorkContext
from herdeck.project_icon_discovery import MAX_ICON_BYTES, icon_hash
from herdeck.protocol import (
    Error,
    Event,
    ProjectIcon,
    Result,
    Snapshot,
    Unknown,
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


def test_decode_snapshot_reads_the_focus_flag_and_defaults_it_off():
    raw = (
        '{"type":"snapshot","server_id":"w","panes":['
        '{"pane_id":"a","agent_type":"claude","status":"blocked","focused":true},'
        '{"pane_id":"b","agent_type":"claude","status":"blocked"}]}'
    )
    msg = decode_inbound(raw)
    assert [s.focused for s in msg.states] == [True, False]


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


def test_decode_snapshot_preserves_valid_native_order():
    raw = (
        '{"type":"snapshot","server_id":"workbox","panes":'
        '[{"pane_id":"w1:p1","agent_type":"claude","label":"api",'
        '"status":"idle","workspace_order":2,"tab_order":5}]}'
    )

    msg = decode_inbound(raw)

    assert msg.states[0].workspace_order == 2
    assert msg.states[0].tab_order == 5


@pytest.mark.parametrize("value", [True, -1, 2.5, "2", None])
def test_decode_snapshot_drops_malformed_native_order(value):
    raw = json.dumps(
        {
            "type": "snapshot",
            "server_id": "workbox",
            "panes": [
                {
                    "pane_id": "w1:p1",
                    "agent_type": "claude",
                    "label": "api",
                    "status": "idle",
                    "workspace_order": value,
                }
            ],
        }
    )

    msg = decode_inbound(raw)

    assert msg.states[0].workspace_order is None


def test_decode_snapshot_preserves_terminal_identity():
    raw = (
        '{"type":"snapshot","server_id":"workbox","panes":'
        '[{"pane_id":"w1:p1","terminal_id":"term-123","agent_type":"claude",'
        '"label":"api","status":"blocked"}]}'
    )

    msg = decode_inbound(raw)

    assert msg.states[0].terminal_id == "term-123"


def test_decode_snapshot_preserves_state_labels():
    raw = (
        '{"type":"snapshot","server_id":"workbox","panes":'
        '[{"pane_id":"w1:p1","agent_type":"claude","label":"api","status":"working",'
        '"state_labels":{"working":"PROBE","idle":"parked"}}]}'
    )

    msg = decode_inbound(raw)

    assert msg.states[0].state_labels == {"working": "PROBE", "idle": "parked"}


def test_decode_snapshot_without_state_labels_defaults_to_empty():
    # An older herdr (or an older bridge) simply omits the field.
    msg = decode_inbound(
        '{"type":"snapshot","server_id":"old","panes":'
        '[{"pane_id":"p1","agent_type":"codex","label":"api","status":"idle"}]}'
    )

    assert msg.states[0].state_labels == {}


def test_decode_snapshot_drops_malformed_state_labels():
    raw = (
        '{"type":"snapshot","server_id":"workbox","panes":'
        '[{"pane_id":"w1:p1","agent_type":"claude","label":"api","status":"working",'
        '"state_labels":{"working":7,"idle":"parked"}},'
        '{"pane_id":"w1:p2","agent_type":"claude","label":"web","status":"idle",'
        '"state_labels":"working"}]}'
    )

    msg = decode_inbound(raw)

    assert msg.states[0].state_labels == {"idle": "parked"}
    assert msg.states[1].state_labels == {}


def test_decode_snapshot_preserves_work_context_and_capabilities():
    raw = (
        '{"type":"snapshot","server_id":"workbox","protocol":2,'
        '"capabilities":["work_context","terminal_preview"],"panes":'
        '[{"pane_id":"w1:p1","agent_type":"codex","label":"api",'
        '"status":"working","title":"Fix issue 123","display_agent":"Codex reviewer",'
        '"work":{"source":"github","item":"repo#123","run":"run-42",'
        '"url":"https://example.test/issues/123"}}]}'
    )

    msg = decode_inbound(raw)

    assert msg.protocol == 2
    assert msg.capabilities == ("work_context", "terminal_preview")
    assert msg.states[0].title == "Fix issue 123"
    assert msg.states[0].display_agent == "Codex reviewer"
    assert msg.states[0].work == WorkContext(
        source="github",
        item="repo#123",
        run="run-42",
        url="https://example.test/issues/123",
    )


def test_decode_legacy_snapshot_defaults_capabilities_and_work_context():
    msg = decode_inbound(
        '{"type":"snapshot","server_id":"old","panes":'
        '[{"pane_id":"p1","agent_type":"codex","label":"api","status":"idle"}]}'
    )

    assert msg.protocol == 1
    assert msg.capabilities == ()
    assert msg.states[0].work == WorkContext()


def test_decode_snapshot_preserves_pane_capabilities():
    msg = decode_inbound(
        '{"type":"snapshot","server_id":"workbox","panes":'
        '[{"pane_id":"w1:p1","agent_type":"codex","label":"api",'
        '"status":"idle","capabilities":["refresh_title",7]}]}'
    )

    assert msg.states[0].capabilities == ("refresh_title",)


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


def test_working_pane_with_waiting_on_derives_waiting():
    from herdeck.model import Status
    from herdeck.protocol import _pane_to_state

    # herdwatch asserts `working` + a label while holding a pane on background
    # work; herdeck surfaces that as the distinct WAITING state.
    held = _pane_to_state("dev", {"pane_id": "p1", "status": "working", "waiting_on": "⏳ ci"})
    assert held.status is Status.WAITING
    assert held.waiting_on == "⏳ ci"
    # genuinely working pane (no label) stays WORKING
    plain = _pane_to_state("dev", {"pane_id": "p1", "status": "working"})
    assert plain.status is Status.WORKING and plain.waiting_on == ""
    # waiting metadata is authoritative for idle/done panes too
    idle = _pane_to_state("dev", {"pane_id": "p1", "status": "idle", "waiting_on": "⏳ x"})
    assert idle.status is Status.WAITING


def test_progress_keeps_working_state_and_metadata():
    from herdeck.protocol import _pane_to_state

    active = _pane_to_state(
        "dev",
        {
            "pane_id": "p1",
            "status": "working",
            "progress": "2/5 Run tests",
            "metadata": {"progress": "2/5 Run tests", "model": "gpt-5"},
        },
    )
    assert active.status is Status.WORKING
    assert active.progress == "2/5 Run tests"
    assert active.metadata["model"] == "gpt-5"


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


def _icon_frame(payload=b"\x89PNG-bytes", **over):
    # ``payload`` (not ``data``): ``over`` may itself override the "data" key.
    msg = {
        "type": "project_icon",
        "server_id": "workbox",
        "hash": icon_hash(payload),
        "mime": "image/png",
        "data": base64.b64encode(payload).decode(),
    }
    msg.update(over)
    return json.dumps(msg)


def test_decode_project_icon_frame():
    data = b"\x89PNG-bytes"
    assert decode_inbound(_icon_frame(data)) == ProjectIcon(
        "workbox", icon_hash(data), "image/png", data
    )


@pytest.mark.parametrize(
    "over",
    [
        {"hash": "XYZ"},
        {"hash": "0" * 16},  # well-formed but does not match the bytes
        {"mime": "text/html"},
        {"mime": ["image/png"]},
        {"data": "not base64!!"},
        {"data": ""},
        {"data": 5},
        {"server_id": None},
    ],
)
def test_decode_malformed_project_icon_raises(over):
    with pytest.raises(ValueError, match="project_icon"):
        decode_inbound(_icon_frame(**over))


def test_decode_project_icon_rejects_oversized_payload():
    with pytest.raises(ValueError, match="project_icon"):
        decode_inbound(_icon_frame(b"x" * (MAX_ICON_BYTES + 1)))


def test_decode_unknown_type_is_tolerated():
    assert decode_inbound('{"type":"future_frame","x":1}') == Unknown("future_frame")


def test_snapshot_carries_project_icon_hash():
    h = icon_hash(b"a")
    raw = json.dumps(
        {
            "type": "snapshot",
            "server_id": "s",
            "panes": [{"pane_id": "p", "status": "idle", "project_icon": h}],
        }
    )
    assert decode_inbound(raw).states[0].project_icon == h


@pytest.mark.parametrize("value", [None, "", "nothex!!", 12, "A" * 16])
def test_snapshot_project_icon_defaults_to_empty(value):
    pane = {"pane_id": "p", "status": "idle"}
    if value is not None:
        pane["project_icon"] = value
    raw = json.dumps({"type": "snapshot", "server_id": "s", "panes": [pane]})
    assert decode_inbound(raw).states[0].project_icon == ""


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("2/5", (2, 5)),
        ("0/0", (0, 0)),
        (" 1 / 3 ", (1, 3)),
        ("5/2", (0, 0)),  # running > total: junk
        ("-1/3", (0, 0)),
        ("1/3/4", (0, 0)),
        ("a/b", (0, 0)),
        ("12345/99999", (0, 0)),
        ("", (0, 0)),
        (None, (0, 0)),
    ],
)
def test_subagents_metadata_token_parses_robustly(token, expected):
    from herdeck.protocol import _pane_to_state

    metadata = {} if token is None else {"subagents": token}
    state = _pane_to_state("dev", {"pane_id": "p1", "status": "working", "metadata": metadata})
    assert (state.subagents_running, state.subagents_total) == expected


def test_subagents_parse_ignores_non_string_values():
    from herdeck.model import parse_subagents_token

    for value in (3, 2.5, ["1/2"], {"r": 1}, b"1/2"):
        assert parse_subagents_token(value) == (0, 0)
