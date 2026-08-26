import asyncio
import contextlib
import json
import os
import sys

import pytest

from herdeck.bridge import (
    HerdrRpcError,
    StubHerdr,
    _herdr_pane_to_wire,
    _is_agent_pane,
    _tabs_by_id,
    _wire_panes,
    _workspaces_by_id,
    _worktrees_by_workspace,
    handle_client_message,
)
from herdeck.decisions import decision_revision


def raw_pane(pane_id="w1:p1", agent="claude", status="blocked", cwd="/home/user/projects/api"):
    """A raw herdr pane (as returned by pane.list)."""
    p = {
        "pane_id": pane_id,
        "workspace_id": "w1",
        "cwd": cwd,
        "foreground_cwd": cwd,
        "agent_status": status,
    }
    if agent is not None:
        p["agent"] = agent
    return p


@pytest.fixture
def herdr():
    return StubHerdr(panes=[raw_pane()])


# --- herdr -> wire mapping ---


def test_herdr_pane_to_wire_maps_fields():
    w = _herdr_pane_to_wire(
        raw_pane(agent="codex", status="working", cwd="/home/user/projects/web")
    )
    assert w == {
        "pane_id": "w1:p1",
        "agent_type": "codex",
        "label": "web",
        "status": "working",
        "project": "web",
        "repo": "web",
        "branch": "",
        "workspace": "",
        "tab": "",
        "waiting_on": "",
        "progress": "",
        "metadata": {},
        "state_labels": {},
        "terminal_id": "",
        "title": "",
        "display_agent": "",
        "work": {"source": "", "item": "", "run": "", "url": ""},
    }


def test_herdr_pane_to_wire_passes_waiting_on_through():
    # herdwatch holds panes via the explicit Herdr metadata token.
    raw = raw_pane(agent="claude", status="working", cwd="/x/api")
    raw["tokens"] = {"waiting_on": "\u23f3 ci"}
    assert _herdr_pane_to_wire(raw)["waiting_on"] == "\u23f3 ci"


def test_herdr_pane_to_wire_passes_state_labels_through():
    # herdr 0.8.2's native per-status labels ride through untouched.
    raw = raw_pane(agent="claude", status="working", cwd="/x/api")
    raw["state_labels"] = {"working": "PROBE", "idle": "parked"}

    assert _herdr_pane_to_wire(raw)["state_labels"] == {"working": "PROBE", "idle": "parked"}


def test_herdr_pane_to_wire_defaults_state_labels_for_older_herdr():
    # A pre-0.8.2 herdr omits the field entirely; absence is not an error.
    assert _herdr_pane_to_wire(raw_pane())["state_labels"] == {}


@pytest.mark.parametrize("bad", ["working", 7, None, ["working"]])
def test_herdr_pane_to_wire_drops_non_object_state_labels(bad):
    raw = raw_pane()
    raw["state_labels"] = bad

    assert _herdr_pane_to_wire(raw)["state_labels"] == {}


def test_herdr_pane_to_wire_drops_malformed_state_label_entries():
    # Cosmetic labels: a bad entry is dropped, the good ones still render.
    raw = raw_pane()
    raw["state_labels"] = {"working": 7, "idle": "parked", 3: "three", "done": None}

    assert _herdr_pane_to_wire(raw)["state_labels"] == {"idle": "parked"}


def test_herdr_pane_to_wire_keeps_only_status_keyed_state_labels():
    # The tile only ever reads the entry for the pane's own status, so unknown
    # keys are dropped -- they can never crowd out the one that matters.
    raw = raw_pane()
    raw["state_labels"] = dict(
        {f"s{i}": f"L{i}" for i in range(40)}, working="probe", blocked="held"
    )

    assert _herdr_pane_to_wire(raw)["state_labels"] == {"working": "probe", "blocked": "held"}


def test_herdr_pane_to_wire_clips_a_long_state_label():
    raw = raw_pane()
    raw["state_labels"] = {"working": "x" * 500}

    assert _herdr_pane_to_wire(raw)["state_labels"] == {"working": "x" * 160}


def test_herdr_pane_to_wire_passes_terminal_identity_through():
    raw = raw_pane()
    raw["terminal_id"] = "term-123"

    assert _herdr_pane_to_wire(raw)["terminal_id"] == "term-123"


def test_herdr_pane_to_wire_exposes_allowlisted_work_context():
    raw = raw_pane()
    raw.update(
        {
            "title": "Fix issue 123",
            "display_agent": "Codex reviewer",
            "tokens": {
                "work_source": "github",
                "work_item": "vaclavik-xyz/persOS#123",
                "work_run": "run-42",
                "work_url": "https://github.com/vaclavik-xyz/persOS/issues/123",
                "summary": "reviewing auth",
            },
        }
    )

    wire = _herdr_pane_to_wire(raw)

    assert wire["title"] == "Fix issue 123"
    assert wire["display_agent"] == "Codex reviewer"
    assert wire["work"] == {
        "source": "github",
        "item": "vaclavik-xyz/persOS#123",
        "run": "run-42",
        "url": "https://github.com/vaclavik-xyz/persOS/issues/123",
    }
    assert wire["metadata"]["summary"] == "reviewing auth"


def test_herdr_pane_to_wire_uses_stripped_terminal_title_as_fallback():
    raw = raw_pane()
    raw["terminal_title_stripped"] = "Review authentication"
    assert _herdr_pane_to_wire(raw)["title"] == "Review authentication"


def test_herdr_pane_to_wire_adds_repo_and_branch_from_worktree():
    raw = raw_pane(agent="claude", status="idle", cwd="/x/macdoktor-crm")
    raw["workspace_id"] = "w1"
    wt_by_ws = _worktrees_by_workspace(
        [{"open_workspace_id": "w1", "label": "macdoktor-crm", "branch": "feat/x"}]
    )
    w = _herdr_pane_to_wire(raw, wt_by_ws)
    assert w["repo"] == "macdoktor-crm" and w["branch"] == "feat/x"


def test_is_agent_pane_filters_plain_shells():
    assert _is_agent_pane(raw_pane()) is True
    # a plain shell pane: no agent, status unknown -> excluded
    shell = {"pane_id": "w9:p1", "agent_status": "unknown", "cwd": "/x"}
    assert _is_agent_pane(shell) is False


def test_wire_panes_filters_and_maps():
    raw = [
        raw_pane("w1:p1", agent="claude", status="blocked"),
        {"pane_id": "w9:p1", "agent_status": "unknown", "cwd": "/x"},
    ]
    out = _wire_panes(raw)
    assert [p["pane_id"] for p in out] == ["w1:p1"]
    assert out[0]["agent_type"] == "claude"


# --- client message handling ---


async def test_list_returns_mapped_filtered_snapshot(herdr):
    out = await handle_client_message(herdr, "workbox", '{"type":"list"}')
    msg = json.loads(out)
    assert msg["type"] == "snapshot"
    assert msg["server_id"] == "workbox"
    assert msg["protocol"] == 3
    assert msg["capabilities"] == [
        "work_context",
        "terminal_preview",
        "metadata_tokens",
        "state_labels",
    ]
    p = msg["panes"][0]
    assert p["pane_id"] == "w1:p1"
    assert p["agent_type"] == "claude"  # mapped from herdr "agent"
    assert p["status"] == "blocked"  # mapped from "agent_status"
    assert p["label"] == "api"  # derived from cwd


async def test_read_returns_result(herdr):
    herdr.detection["w1:p1"] = "Allow edit to config.py?"
    out = await handle_client_message(
        herdr, "workbox", '{"type":"read","req":"r1","pane_id":"w1:p1","source":"detection"}'
    )
    msg = json.loads(out)
    assert msg["type"] == "result"
    assert msg["req"] == "r1"
    assert msg["data"]["text"] == "Allow edit to config.py?"
    assert msg["data"]["pane_id"] == "w1:p1"


async def test_act_sends_keys_when_blocked(herdr):
    out = await handle_client_message(
        herdr, "workbox", '{"type":"act","req":"r2","pane_id":"w1:p1","keys":["1","enter"]}'
    )
    msg = json.loads(out)
    assert msg["type"] == "result"
    assert herdr.sent == [("w1:p1", ["1", "enter"])]


async def test_act_skipped_when_not_blocked(herdr):
    herdr.panes[0]["agent_status"] = "working"
    out = await handle_client_message(
        herdr, "workbox", '{"type":"act","req":"r3","pane_id":"w1:p1","keys":["1"]}'
    )
    msg = json.loads(out)
    assert msg["data"]["skipped"] is True
    assert herdr.sent == []


async def test_act_unguarded_sends_even_when_not_blocked(herdr):
    herdr.panes[0]["agent_status"] = "working"
    out = await handle_client_message(
        herdr,
        "workbox",
        '{"type":"act","req":"r9","pane_id":"w1:p1","keys":["ctrl+c"],"guard":false}',
    )
    msg = json.loads(out)
    assert msg["data"]["sent"] is True
    assert herdr.sent == [("w1:p1", ["ctrl+c"])]


async def test_act_rejects_recycled_pane_identity(herdr):
    herdr.panes[0]["terminal_id"] = "term-new"

    out = await handle_client_message(
        herdr,
        "workbox",
        '{"type":"act","req":"r10","pane_id":"w1:p1",'
        '"terminal_id":"term-old","keys":["1"],"guard":false}',
    )

    msg = json.loads(out)
    assert msg["data"] == {"skipped": True, "message": "agent identity changed"}
    assert herdr.sent == []


# --- broadcast fan-out (snapshots) ---


async def test_broadcast_fans_out_snapshots_to_all_clients():
    from herdeck.bridge import _broadcast

    sent_a, sent_b = [], []

    class FakeWS:
        def __init__(self, log):
            self._log = log

        async def send(self, msg):
            self._log.append(msg)

    clients = {
        FakeWS(sent_a): asyncio.Lock(),
        FakeWS(sent_b): asyncio.Lock(),
    }

    async def stream():
        yield [
            {
                "pane_id": "w1:p1",
                "agent_type": "claude",
                "label": "api",
                "status": "blocked",
                "project": "api",
            }
        ]

    await _broadcast(stream(), clients, "workbox")
    assert len(sent_a) == 1 and len(sent_b) == 1
    msg = json.loads(sent_a[0])
    assert msg["type"] == "snapshot"
    assert msg["server_id"] == "workbox"
    assert msg["panes"][0]["pane_id"] == "w1:p1"


async def test_broadcast_survives_a_dead_client():
    from herdeck.bridge import _broadcast

    good = []

    class GoodWS:
        async def send(self, msg):
            good.append(msg)

    class DeadWS:
        async def send(self, msg):
            raise RuntimeError("closed")

    clients = {GoodWS(): asyncio.Lock(), DeadWS(): asyncio.Lock()}

    async def stream():
        yield [{"pane_id": "p", "status": "working"}]

    await _broadcast(stream(), clients, "s")  # must not raise
    assert len(good) == 1


# --- poll-diff snapshot source ---


async def test_herdr_events_yields_full_list_on_change_and_removal():
    from herdeck.bridge import HerdrEvents

    seq = [
        [
            raw_pane("w1:p1", agent="claude", status="idle"),
            raw_pane("w1:p2", agent="codex", status="working"),
        ],
        [
            raw_pane("w1:p1", agent="claude", status="blocked"),
            raw_pane("w1:p2", agent="codex", status="working"),
        ],  # p1 changed
        [raw_pane("w1:p1", agent="claude", status="blocked")],  # p2 removed
    ]

    class FakeHerdr:
        def __init__(self):
            self.calls = 0

        async def snapshot(self):
            i = min(self.calls, len(seq) - 1)
            self.calls += 1
            return {"agents": seq[i]}

        async def worktrees(self, workspace_ids=None):
            return []

    gen = HerdrEvents(FakeHerdr(), poll_interval=0).stream()
    first = await gen.__anext__()
    assert {p["pane_id"] for p in first} == {"w1:p1", "w1:p2"}
    second = await gen.__anext__()
    assert [p for p in second if p["pane_id"] == "w1:p1"][0]["status"] == "blocked"
    third = await gen.__anext__()
    assert {p["pane_id"] for p in third} == {"w1:p1"}  # removal reflected
    await gen.aclose()


async def test_herdr_events_preserves_last_good_state_across_malformed_snapshot():
    from herdeck.bridge import HerdrEvents

    class FlakyHerdr:
        def __init__(self):
            self.calls = 0

        async def snapshot(self):
            self.calls += 1
            if self.calls == 1:
                return {"agents": [raw_pane("w1:p1", status="idle")]}
            if self.calls == 2:
                raise RuntimeError("session.snapshot snapshot agents must be a list")
            return {"agents": [raw_pane("w1:p1", status="blocked")]}

        async def worktrees(self, workspace_ids=None):
            return []

    gen = HerdrEvents(FlakyHerdr(), poll_interval=0).stream()

    first = await gen.__anext__()
    second = await gen.__anext__()

    assert first[0]["status"] == "idle"
    assert second[0]["status"] == "blocked"
    await gen.aclose()


# --- rpc retry semantics ---


async def test_rpc_retries_reads_but_not_send_keys(monkeypatch):
    import asyncio as _asyncio

    from herdeck.bridge import SocketHerdr

    writes = []

    class FakeWriter:
        def write(self, data):
            writes.append(data)

        async def drain(self):
            pass

        def is_closing(self):
            return False

        def close(self):
            pass

    class FakeReader:
        async def readline(self):
            return b""  # always EOF -> triggers retry path

    async def fake_conn(path, **kwargs):
        return FakeReader(), FakeWriter()

    monkeypatch.setattr(_asyncio, "open_unix_connection", fake_conn)

    h = SocketHerdr("/nonexistent.sock")

    writes.clear()
    with pytest.raises(ConnectionError):
        await h.get_pane("w1:p1")  # idempotent -> retried
    assert len(writes) == 2

    writes.clear()
    with pytest.raises(ConnectionError):
        await h.send_keys("w1:p1", ["1"])  # non-idempotent -> NOT retried
    assert len(writes) == 1


async def test_rpc_error_envelope_raises(monkeypatch):
    import asyncio as _asyncio

    from herdeck.bridge import SocketHerdr

    class FakeWriter:
        def write(self, data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    class FakeReader:
        async def readline(self):
            return b'{"id":"b","error":{"message":"pane not found"}}\n'

    async def fake_conn(path, **kwargs):
        return FakeReader(), FakeWriter()

    monkeypatch.setattr(_asyncio, "open_unix_connection", fake_conn)

    h = SocketHerdr("/tmp/herdr.sock")
    with pytest.raises(RuntimeError, match="pane not found"):
        await h.read_pane("w1:p1", "detection")


async def test_rpc_error_raises_herdr_rpc_error_with_code(monkeypatch):
    import asyncio as _asyncio

    from herdeck.bridge import SocketHerdr

    class FakeWriter:
        def write(self, data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    class FakeReader:
        async def readline(self):
            return b'{"id":"b","error":{"code":"agent_blocked","message":"pane is blocked"}}\n'

    async def fake_conn(path, **kwargs):
        return FakeReader(), FakeWriter()

    monkeypatch.setattr(_asyncio, "open_unix_connection", fake_conn)

    h = SocketHerdr("/tmp/herdr.sock")
    with pytest.raises(HerdrRpcError) as exc_info:
        await h.read_pane("w1:p1", "detection")

    exc = exc_info.value
    assert exc.code == "agent_blocked"
    assert exc.message == "pane is blocked"
    assert str(exc) == "herdr RPC pane.read failed: pane is blocked"


async def test_rpc_error_non_dict_error_yields_empty_code(monkeypatch):
    import asyncio as _asyncio

    from herdeck.bridge import SocketHerdr

    class FakeWriter:
        def write(self, data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    class FakeReader:
        async def readline(self):
            return b'{"id":"b","error":"boom"}\n'

    async def fake_conn(path, **kwargs):
        return FakeReader(), FakeWriter()

    monkeypatch.setattr(_asyncio, "open_unix_connection", fake_conn)

    h = SocketHerdr("/tmp/herdr.sock")
    with pytest.raises(HerdrRpcError) as exc_info:
        await h.read_pane("w1:p1", "detection")

    assert exc_info.value.code == ""


async def test_rpc_has_absolute_timeout(monkeypatch):
    import asyncio as _asyncio

    from herdeck.bridge import SocketHerdr

    class FakeWriter:
        def write(self, data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    class HangingReader:
        async def readline(self):
            await _asyncio.Event().wait()

    async def fake_conn(path, **kwargs):
        return HangingReader(), FakeWriter()

    monkeypatch.setattr(_asyncio, "open_unix_connection", fake_conn)
    h = SocketHerdr("/tmp/herdr.sock", timeout=0.01)

    with pytest.raises(TimeoutError, match="herdr RPC pane.list timed out"):
        await h.list_panes()


async def test_rpc_opens_stream_with_explicit_line_limit(monkeypatch):
    import asyncio as _asyncio

    from herdeck.bridge import SocketHerdr

    seen = []

    class FakeWriter:
        def write(self, data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    class FakeReader:
        async def readline(self):
            return b'{"result":{"panes":[]}}\n'

    async def fake_conn(path, **kwargs):
        seen.append(kwargs)
        return FakeReader(), FakeWriter()

    monkeypatch.setattr(_asyncio, "open_unix_connection", fake_conn)

    await SocketHerdr("/tmp/herdr.sock", line_limit=1234).list_panes()

    assert seen == [{"limit": 1234}]


def test_subscription_ack_requires_started_result():
    from herdeck.bridge import _validate_subscription_ack

    with pytest.raises(RuntimeError, match="subscription_started"):
        _validate_subscription_ack(b'{"result":{"type":"ok"}}\n')


@pytest.mark.parametrize(
    "line",
    [
        b"[]\n",
        b'{"event":42,"data":{}}\n',
        b'{"event":"pane.created","data":[]}\n',
        b"not-json\n",
    ],
)
def test_event_decoder_rejects_malformed_payloads(line):
    from herdeck.bridge import _decode_event_line

    assert _decode_event_line(line) is None


def test_reconnect_backoff_grows_on_failure_and_resets_after_handshake():
    from herdeck.bridge import _reconnect_backoff

    assert _reconnect_backoff(0.5, base=0.5, maximum=2.0, connected=False) == (0.5, 1.0)
    assert _reconnect_backoff(1.0, base=0.5, maximum=2.0, connected=False) == (1.0, 2.0)
    assert _reconnect_backoff(2.0, base=0.5, maximum=2.0, connected=False) == (2.0, 2.0)
    assert _reconnect_backoff(2.0, base=0.5, maximum=2.0, connected=True) == (0.5, 0.5)


async def test_snapshot_rejects_missing_agents_instead_of_clearing_fleet():
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")

    async def fake_rpc(method, params, *, retry=True):
        return {"result": {"snapshot": {"panes": []}}}

    h._rpc = fake_rpc

    with pytest.raises(RuntimeError, match="snapshot agents must be a list"):
        await h.snapshot()


def test_bridge_main_rejects_empty_token(monkeypatch):
    from herdeck import bridge

    async def should_not_start(*args, **kwargs):
        raise AssertionError("serve should not start with empty token")

    monkeypatch.setenv("HERDR_SOCKET", "/tmp/herdr.sock")
    monkeypatch.setenv("HERDECK_TOKEN", "   ")
    monkeypatch.setattr(bridge, "serve", should_not_start)

    with pytest.raises(SystemExit, match="HERDECK_TOKEN must not be empty"):
        bridge.main()


def test_bridge_token_file_must_be_private_and_never_echoes_value(tmp_path, monkeypatch):
    from herdeck.bridge import load_bridge_token

    token_file = tmp_path / "bridge-token"
    token_file.write_text("file-secret\n")
    token_file.chmod(0o600)
    monkeypatch.delenv("HERDECK_TOKEN", raising=False)
    monkeypatch.setenv("HERDECK_TOKEN_FILE", str(token_file))

    assert load_bridge_token() == "file-secret"

    token_file.chmod(0o644)
    with pytest.raises(SystemExit, match="permissions must be 0600") as exc:
        load_bridge_token()
    assert "file-secret" not in str(exc.value)


def test_bridge_token_file_takes_precedence_over_legacy_environment(tmp_path, monkeypatch):
    from herdeck.bridge import load_bridge_token

    token_file = tmp_path / "bridge-token"
    token_file.write_text("file-secret\n")
    token_file.chmod(0o600)
    monkeypatch.setenv("HERDECK_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("HERDECK_TOKEN", "stale-inline-secret")

    assert load_bridge_token() == "file-secret"


async def test_focus_calls_herdr_focus_agent(herdr):
    out = await handle_client_message(
        herdr, "workbox", '{"type":"focus","req":"f1","pane_id":"w1:p1"}'
    )
    msg = json.loads(out)
    assert msg["data"]["focused"] is True
    assert herdr.focused == ["w1:p1"]


async def test_push_event_wakes_stream_before_poll():
    import asyncio

    from herdeck.bridge import HerdrEvents

    seq = [
        [raw_pane("w1:p1", agent="claude", status="idle")],
        [raw_pane("w1:p1", agent="claude", status="blocked")],
    ]

    class FakeHerdr:
        def __init__(self):
            self.calls = 0

        async def snapshot(self):
            i = min(self.calls, len(seq) - 1)
            self.calls += 1
            return {"agents": seq[i]}

        async def worktrees(self, workspace_ids=None):
            return []

    ev = HerdrEvents(FakeHerdr(), poll_interval=100)  # would block without a wake
    gen = ev.stream()
    first = await gen.__anext__()
    assert first[0]["status"] == "idle"
    ev._wake.set()  # simulate a herdr push event
    second = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert second[0]["status"] == "blocked"
    await gen.aclose()


async def test_send_text_calls_herdr(herdr):
    out = await handle_client_message(
        herdr, "workbox", '{"type":"send_text","req":"s1","pane_id":"w1:p1","text":"continue"}'
    )
    msg = json.loads(out)
    assert msg["data"]["sent"] is True
    assert herdr.sent == [("w1:p1", "continue")]


async def test_send_text_falls_back_to_answer_blocked_on_agent_blocked(herdr):
    async def blocked_send_text(pane_id, text):
        raise HerdrRpcError("agent.prompt", "agent_blocked", "pane is blocked")

    herdr.send_text = blocked_send_text

    out = await handle_client_message(
        herdr, "workbox", '{"type":"send_text","req":"s2","pane_id":"w1:p1","text":"continue"}'
    )

    assert json.loads(out)["data"] == {"sent": True}
    assert herdr.sent == [("w1:p1", "continue")]


async def test_send_text_propagates_non_agent_blocked_error(herdr):
    async def stalled_send_text(pane_id, text):
        raise HerdrRpcError("agent.prompt", "agent_prompt_stalled", "no state change")

    herdr.send_text = stalled_send_text

    with pytest.raises(HerdrRpcError, match="no state change"):
        await handle_client_message(
            herdr, "workbox", '{"type":"send_text","req":"s3","pane_id":"w1:p1","text":"continue"}'
        )
    assert herdr.sent == []


@pytest.mark.parametrize(
    "text",
    [
        "yes, approve\nsomething else entirely",
        # over-broad normalisation (e.g. stripping ALL newlines, or per-line
        # strip()) would pass this case even though the embedded newline is
        # still there once the trailing one is trimmed off:
        "yes, approve\nsomething else entirely\r\n",
    ],
)
async def test_send_text_multiline_fallback_is_skipped_not_typed(herdr, text):
    """answer_blocked has no bracketed-paste framing: an embedded newline
    would submit mid-typing (e.g. a two-line Telegram reply to a blocked
    agent), so the multiline fallback must be rejected, not typed."""

    async def blocked_send_text(pane_id, text):
        raise HerdrRpcError("agent.prompt", "agent_blocked", "pane is blocked")

    herdr.send_text = blocked_send_text

    out = await handle_client_message(
        herdr,
        "workbox",
        json.dumps({"type": "send_text", "req": "s4", "pane_id": "w1:p1", "text": text}),
    )

    assert json.loads(out)["data"] == {"skipped": True, "message": "untypeable_blocked_answer"}
    assert herdr.sent == []  # never typed into the pane; StubHerdr.answer_blocked has no guard
    # of its own, so this only passes because handle_client_message checked first.


@pytest.mark.parametrize(
    "text",
    [
        "yes\r\n",
        # rstrip() (not rstrip("\r\n")) is what makes these two acceptable:
        # a newline with trailing spaces after it, and a plain trailing space.
        "yes\n  ",
        "yes ",
    ],
)
async def test_send_text_fallback_trims_trailing_newline_before_checking(herdr, text):
    """A lone trailing newline (e.g. a pasted Telegram reply) is common,
    legitimate input, not a multiline answer, and must not be dropped."""

    async def blocked_send_text(pane_id, text):
        raise HerdrRpcError("agent.prompt", "agent_blocked", "pane is blocked")

    herdr.send_text = blocked_send_text

    out = await handle_client_message(
        herdr,
        "workbox",
        json.dumps({"type": "send_text", "req": "s5", "pane_id": "w1:p1", "text": text}),
    )

    assert json.loads(out)["data"] == {"sent": True}
    # the *normalised* text is what gets typed, not the raw payload
    assert herdr.sent == [("w1:p1", "yes")]


@pytest.mark.parametrize("text", ["\n", "\r\n\r\n", "   ", "\ue000"])
async def test_send_text_fallback_rejects_empty_answer(herdr, text):
    """An empty normalised answer must never reach answer_blocked: typing
    nothing and pressing enter would silently confirm the blocked dialog's
    default choice — this is a safety guard, not a formatting nicety."""

    async def blocked_send_text(pane_id, text):
        raise HerdrRpcError("agent.prompt", "agent_blocked", "pane is blocked")

    herdr.send_text = blocked_send_text

    out = await handle_client_message(
        herdr,
        "workbox",
        json.dumps({"type": "send_text", "req": "s6", "pane_id": "w1:p1", "text": text}),
    )

    assert json.loads(out)["data"] == {"skipped": True, "message": "empty_blocked_answer"}
    assert herdr.sent == []  # nothing typed, no accidental "enter" against the default choice


@pytest.mark.parametrize(
    "text",
    [
        "\u200b",  # ZERO WIDTH SPACE
        "\ufeff",  # BOM / ZERO WIDTH NO-BREAK SPACE
        "\u2060",  # WORD JOINER
        "\u00ad",  # SOFT HYPHEN
        "\u180e",  # MONGOLIAN VOWEL SEPARATOR
        "\u200d",  # ZERO WIDTH JOINER
        "\u0301",  # COMBINING ACUTE ACCENT: printable, but no base to sit on
        " \u200b\n",  # invisible plus real whitespace, as a paste would carry it
    ],
)
async def test_send_text_fallback_rejects_invisible_answer(herdr, text):
    """str.isspace() is False for U+200B/U+FEFF/U+2060/U+00AD/U+180E and they
    are all >= 32, so rstrip() leaves them and an ord-based control-char check
    waves them through — yet they type nothing, so the enter that follows
    confirms the dialog's default. They are as empty as a bare newline."""

    async def blocked_send_text(pane_id, text):
        raise HerdrRpcError("agent.prompt", "agent_blocked", "pane is blocked")

    herdr.send_text = blocked_send_text

    out = await handle_client_message(
        herdr,
        "workbox",
        json.dumps({"type": "send_text", "req": "s7", "pane_id": "w1:p1", "text": text}),
    )

    assert json.loads(out)["data"] == {"skipped": True, "message": "empty_blocked_answer"}
    assert herdr.sent == []  # nothing typed, no accidental "enter" against the default choice


@pytest.mark.parametrize(
    "text",
    [
        "yes\u2028no",  # LINE SEPARATOR
        "yes\u2029no",  # PARAGRAPH SEPARATOR
        "yes\u0085no",  # NEL (a C1 control: isspace(), but ord > 127)
        "yes\u007fno",  # DEL
        "yes\tno",  # TAB: a keystroke the dialog acts on, not literal text
        # json.loads accepts a lone surrogate escape off the client socket; it
        # cannot be typed and cannot be re-encoded for the herdr RPC either.
        "yes\ud800",
    ],
)
async def test_send_text_fallback_rejects_embedded_untypeable_char(herdr, text):
    """Three reasons live on this side of the split. U+2028/U+2029/U+0085 are
    isspace() but ord > 127, so an ord-based control-char check waves them
    through, and a terminal may treat them as a line break: a submit halfway
    through the answer. TAB and DEL were always refused by the ord-based check
    and stay refused, because the dialog acts on them as keystrokes instead of
    drawing them. A lone surrogate is refused for an unrelated reason:
    nothing types it, and re-encoding it for the herdr RPC yields JSON no
    strict parser reads."""

    async def blocked_send_text(pane_id, text):
        raise HerdrRpcError("agent.prompt", "agent_blocked", "pane is blocked")

    herdr.send_text = blocked_send_text

    out = await handle_client_message(
        herdr,
        "workbox",
        json.dumps({"type": "send_text", "req": "s8", "pane_id": "w1:p1", "text": text}),
    )

    assert json.loads(out)["data"] == {"skipped": True, "message": "untypeable_blocked_answer"}
    assert herdr.sent == []


@pytest.mark.parametrize(
    "text",
    [
        "yes",
        "1",
        "ano, pokra\u010duj",
        "e\u0301clair \U0001f600",  # combining mark ON a base character, and emoji
        "v\u00a0po\u0159\u00e1dku",  # NBSP, as Czech typography and rich-text pastes insert it
        "\U0001f469\u200d\U0001f4bb hotovo",  # U+200D joining an emoji sequence
        "yes\u200bno",  # invisible, but the answer still carries legible text
    ],
)
async def test_send_text_fallback_still_accepts_legible_answers(herdr, text):
    """The guard must not swallow ordinary answers. Only what a terminal acts
    on (Cc/Zl/Zp) and what cannot be encoded at all (Cs) are refused; a
    non-ASCII space or a zero-width joiner inside legible text is typed exactly
    as written, rather than being dropped under a message that names neither
    the character nor the reason."""

    async def blocked_send_text(pane_id, text):
        raise HerdrRpcError("agent.prompt", "agent_blocked", "pane is blocked")

    herdr.send_text = blocked_send_text

    out = await handle_client_message(
        herdr,
        "workbox",
        json.dumps({"type": "send_text", "req": "s9", "pane_id": "w1:p1", "text": text}),
    )

    assert json.loads(out)["data"] == {"sent": True}
    assert herdr.sent == [("w1:p1", text)]


@pytest.mark.parametrize(
    ("text", "typed"),
    [
        ("\ufeff1", "1"),  # BOM-prefixed paste answering a numbered dialog
        ("1\u200b", "1"),
        ("1\u200b\n", "1"),  # rstrip() takes the newline and leaves the ZWSP behind
        ("\u200byes", "yes"),  # the legible run starts only after the invisible
        ("  yes  ", "yes"),
        ("1\ufe0f\u20e3", "1"),  # keycap emoji off a phone keyboard: VS16 + U+20E3 are marks
    ],
)
async def test_send_text_fallback_trims_edge_invisibles(herdr, text, typed):
    """An invisible glued to the edge of a real answer is worse than one on its
    own: "\ufeff1" is legible enough to pass the empty check, gets typed, and
    then a numbered dialog does not match it — so the enter confirms the
    default. Trim the illegible edges instead, as with trailing whitespace."""

    async def blocked_send_text(pane_id, text):
        raise HerdrRpcError("agent.prompt", "agent_blocked", "pane is blocked")

    herdr.send_text = blocked_send_text

    out = await handle_client_message(
        herdr,
        "workbox",
        json.dumps({"type": "send_text", "req": "s10", "pane_id": "w1:p1", "text": text}),
    )

    assert json.loads(out)["data"] == {"sent": True}
    assert herdr.sent == [("w1:p1", typed)]


async def test_guarded_choice_rechecks_prompt_and_blocked_status_before_send(herdr):
    herdr.panes[0]["terminal_id"] = "term-1"
    prompt = "Choose:\n1. Continue\n2. Explain first"
    herdr.detection["w1:p1"] = prompt
    revision = decision_revision("workbox", "w1:p1", "term-1", prompt)

    out = await handle_client_message(
        herdr,
        "workbox",
        json.dumps(
            {
                "type": "choose_if_blocked",
                "req": "c1",
                "pane_id": "w1:p1",
                "terminal_id": "term-1",
                "choice": "2",
                "decision_revision": revision,
            }
        ),
    )

    assert json.loads(out)["data"] == {"sent": True}
    assert herdr.sent == [("w1:p1", "2")]


async def test_choose_if_blocked_uses_answer_blocked_not_send_text(herdr):
    async def fail_send_text(pane_id, text):
        raise AssertionError("choose_if_blocked must use answer_blocked, not send_text")

    herdr.send_text = fail_send_text

    prompt = "Choose:\n1. Continue\n2. Explain first"
    herdr.detection["w1:p1"] = prompt

    out = await handle_client_message(
        herdr,
        "workbox",
        json.dumps(
            {
                "type": "choose_if_blocked",
                "req": "c-ab",
                "pane_id": "w1:p1",
                "choice": "2",
                "decision_revision": decision_revision("workbox", "w1:p1", "", prompt),
            }
        ),
    )

    assert json.loads(out)["data"] == {"sent": True}
    assert herdr.sent == [("w1:p1", "2")]


async def test_guarded_choice_rejects_changed_prompt(herdr):
    prompt = "Choose:\n1. Continue\n2. Different action"
    herdr.detection["w1:p1"] = prompt

    out = await handle_client_message(
        herdr,
        "workbox",
        json.dumps(
            {
                "type": "choose_if_blocked",
                "req": "c2",
                "pane_id": "w1:p1",
                "choice": "2",
                "decision_revision": decision_revision(
                    "workbox", "w1:p1", "", "Choose:\n1. Continue\n2. Explain first"
                ),
            }
        ),
    )

    assert json.loads(out)["data"] == {"skipped": True, "message": "stale_choice"}
    assert herdr.sent == []


async def test_guarded_choice_rejects_missing_option_with_valid_revision(herdr):
    prompt = "Choose:\n1. Continue\n2. Explain first"
    herdr.detection["w1:p1"] = prompt

    out = await handle_client_message(
        herdr,
        "workbox",
        json.dumps(
            {
                "type": "choose_if_blocked",
                "req": "c-missing",
                "pane_id": "w1:p1",
                "choice": "999",
                "decision_revision": decision_revision("workbox", "w1:p1", "", prompt),
            }
        ),
    )

    assert json.loads(out)["data"] == {"skipped": True, "message": "stale_choice"}
    assert herdr.sent == []


async def test_guarded_choice_rejects_status_change_during_prompt_read(herdr):
    class StatusChangingHerdr(StubHerdr):
        async def read_pane(self, pane_id, source):
            text = await super().read_pane(pane_id, source)
            self.panes[0]["agent_status"] = "working"
            return text

    changing = StatusChangingHerdr([raw_pane()])
    prompt = "Choose:\n1. Continue"
    changing.detection["w1:p1"] = prompt

    out = await handle_client_message(
        changing,
        "workbox",
        json.dumps(
            {
                "type": "choose_if_blocked",
                "req": "c3",
                "pane_id": "w1:p1",
                "choice": "1",
                "decision_revision": decision_revision("workbox", "w1:p1", "", prompt),
            }
        ),
    )

    assert json.loads(out)["data"] == {"skipped": True, "message": "not_blocked"}
    assert changing.sent == []


async def test_start_calls_herdr(herdr):
    out = await handle_client_message(
        herdr, "workbox", '{"type":"start","req":"n1","name":"claude","argv":["claude"]}'
    )
    msg = json.loads(out)
    assert msg["data"]["started"] is True
    assert herdr.started == [("claude", ["claude"])]


def test_herdr_pane_to_wire_adds_workspace_and_tab_labels():
    raw = raw_pane(agent="claude", status="working")
    raw["workspace_id"] = "w2"
    raw["tab_id"] = "w2:t3"
    ws_by_id = _workspaces_by_id([{"workspace_id": "w2", "label": "herdeck"}])
    tab_by_id = _tabs_by_id([{"tab_id": "w2:t3", "label": "2"}])
    w = _herdr_pane_to_wire(raw, None, ws_by_id, tab_by_id)
    assert w["workspace"] == "herdeck"
    assert w["tab"] == "2"


def test_herdr_pane_to_wire_blank_workspace_tab_when_lookup_missing():
    raw = raw_pane(agent="claude", status="idle")
    raw["workspace_id"] = "w9"
    raw["tab_id"] = "w9:t1"
    w = _herdr_pane_to_wire(raw, None, {}, {})
    # never fall back to the raw id
    assert w["workspace"] == ""
    assert w["tab"] == ""


def test_workspaces_and_tabs_by_id_index_label():
    assert _workspaces_by_id([{"workspace_id": "w2", "label": "herdeck"}]) == {"w2": "herdeck"}
    assert _tabs_by_id([{"tab_id": "w2:t1", "label": "1"}]) == {"w2:t1": "1"}
    # entries without an id are skipped
    assert _workspaces_by_id([{"label": "x"}]) == {}
    assert _tabs_by_id([{"label": "x"}]) == {}


async def test_list_snapshot_includes_workspace_and_tab():
    panes = [
        {
            "pane_id": "w2:p1",
            "workspace_id": "w2",
            "tab_id": "w2:t1",
            "cwd": "/home/user/projects/herdeck",
            "foreground_cwd": "/home/user/projects/herdeck",
            "agent": "claude",
            "agent_status": "blocked",
        }
    ]
    herdr = StubHerdr(
        panes=panes,
        workspaces=[{"workspace_id": "w2", "label": "herdeck"}],
        tabs=[{"tab_id": "w2:t1", "label": "1"}],
    )
    out = await handle_client_message(herdr, "local", '{"type":"list"}')
    p = json.loads(out)["panes"][0]
    assert p["workspace"] == "herdeck"
    assert p["tab"] == "1"


# --- snapshot RPC concurrency + label caching (audit: bridge-parallel-rpcs) --


async def test_wired_snapshot_reads_labels_from_snapshot():
    pane = raw_pane("w2:p1", agent="claude", status="blocked")
    pane["workspace_id"] = "w2"
    pane["tab_id"] = "w2:t1"
    stub = StubHerdr(
        panes=[pane],
        workspaces=[{"workspace_id": "w2", "label": "herdeck"}],
        tabs=[{"tab_id": "w2:t1", "label": "1"}],
    )
    from herdeck.bridge import _wired_snapshot

    out = await _wired_snapshot(stub)
    assert out[0]["workspace"] == "herdeck"
    assert out[0]["tab"] == "1"


async def test_wired_snapshot_keeps_agent_pane_without_agent_key():
    # herdr's agents list should already be agent-only; the _is_agent_pane belt
    # must keep a row that carries only a live agent_status.
    pane = {
        "pane_id": "w1:p9",
        "workspace_id": "w1",
        "cwd": "/x/api",
        "foreground_cwd": "/x/api",
        "agent_status": "working",
    }
    stub = StubHerdr(panes=[pane])
    from herdeck.bridge import _wired_snapshot

    out = await _wired_snapshot(stub)
    assert [p["pane_id"] for p in out] == ["w1:p9"]


async def test_fetch_worktrees_degrades_to_empty_on_error():
    from herdeck.bridge import _fetch_worktrees

    class BrokenHerdr:
        async def worktrees(self, workspace_ids=None):
            raise RuntimeError("worktree.list failed")

    assert await _fetch_worktrees(BrokenHerdr(), ["w1"]) == []


async def test_stream_caches_worktrees_across_status_wakes():
    import asyncio

    from herdeck.bridge import HerdrEvents

    class CountingHerdr:
        def __init__(self):
            self.panes = [raw_pane("w1:p1", agent="claude", status="idle")]
            self.worktree_calls = 0

        async def snapshot(self):
            return {"agents": self.panes}

        async def worktrees(self, workspace_ids=None):
            self.worktree_calls += 1
            return []

    stub = CountingHerdr()
    ev = HerdrEvents(stub, poll_interval=100)  # would block without a wake
    gen = ev.stream()
    await gen.__anext__()
    assert stub.worktree_calls == 1
    # a status-change push wake must NOT refetch the worktree list
    stub.panes = [raw_pane("w1:p1", agent="claude", status="blocked")]
    ev._wake.set()
    await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert stub.worktree_calls == 1
    # a fleet event (what _listen flags) marks worktrees stale -> refetched
    stub.panes = stub.panes + [raw_pane("w1:p2", agent="codex", status="idle")]
    ev._worktrees_stale = True
    ev._wake.set()
    await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert stub.worktree_calls == 2
    await gen.aclose()


async def test_stream_refreshes_worktrees_by_age_when_wakes_starve_the_poll(monkeypatch):
    """Constant status wakes keep the slow-poll timeout from firing; the cached
    worktrees must still refresh once older than poll_interval (a branch switch
    inside an existing worktree emits no event)."""
    import asyncio

    from herdeck import bridge as bridge_mod
    from herdeck.bridge import HerdrEvents

    t = [0.0]
    monkeypatch.setattr(bridge_mod, "_monotonic", lambda: t[0])

    class CountingHerdr:
        def __init__(self):
            self.panes = [raw_pane("w1:p1", agent="claude", status="idle")]
            self.worktree_calls = 0

        async def snapshot(self):
            return {"agents": self.panes}

        async def worktrees(self, workspace_ids=None):
            self.worktree_calls += 1
            return []

    stub = CountingHerdr()
    ev = HerdrEvents(stub, poll_interval=5.0)
    gen = ev.stream()
    await gen.__anext__()  # worktrees fetched at t=0
    assert stub.worktree_calls == 1
    stub.panes = [raw_pane("w1:p1", agent="claude", status="blocked")]
    t[0] = 1.0
    ev._wake.set()  # young cache + status wake -> no refetch
    await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert stub.worktree_calls == 1
    stub.panes = [raw_pane("w1:p1", agent="claude", status="working")]
    t[0] = 6.0  # cache older than poll_interval; wake is still a status event
    ev._wake.set()
    await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert stub.worktree_calls == 2
    await gen.aclose()


async def test_broadcast_stalled_client_does_not_starve_others():
    """One backpressured client must not delay snapshots for everyone else
    (audit: bridge-broadcast-isolate)."""
    import asyncio

    from herdeck.bridge import _broadcast

    got = []

    class FastWs:
        async def send(self, msg):
            got.append(msg)

    class StalledWs:
        def __init__(self):
            self.closed = False

        async def send(self, msg):
            await asyncio.sleep(3600)  # full TCP buffer: send never returns

        async def close(self, code=None, reason=None):
            self.closed = True

    async def stream():
        yield [{"pane_id": "p1"}]
        yield [{"pane_id": "p2"}]

    stalled = StalledWs()
    clients = {FastWs(): asyncio.Lock(), stalled: asyncio.Lock()}
    # patch the per-client timeout small for the test
    import herdeck.bridge as bridge_mod

    orig = bridge_mod._BROADCAST_SEND_TIMEOUT
    bridge_mod._BROADCAST_SEND_TIMEOUT = 0.05
    try:
        await asyncio.wait_for(_broadcast(stream(), clients, "s"), timeout=5.0)
    finally:
        bridge_mod._BROADCAST_SEND_TIMEOUT = orig
    assert len(got) == 2  # the healthy client received every snapshot
    assert stalled.closed  # the stalled one was dropped to reconnect cleanly


async def test_wired_snapshot_queries_worktrees_for_agent_workspaces():
    from herdeck.bridge import StubHerdr, _wired_snapshot

    pane_a = raw_pane("wA:p1", agent="claude", status="working")
    pane_a["workspace_id"] = "wA"
    pane_b = raw_pane("wB:p1", agent="codex", status="idle")
    pane_b["workspace_id"] = "wB"
    stub = StubHerdr(
        [pane_a, pane_b],
        worktrees=[
            {"open_workspace_id": "wA", "label": "repo-a", "branch": "main"},
            {"open_workspace_id": "wB", "label": "repo-b", "branch": "fix/x"},
        ],
    )
    panes = await _wired_snapshot(stub)
    # worktrees were asked about exactly the agent workspaces (sorted, unique)
    assert stub.worktree_queries == [["wA", "wB"]]
    by_id = {p["pane_id"]: p for p in panes}
    assert by_id["wA:p1"]["branch"] == "main"
    assert by_id["wB:p1"]["branch"] == "fix/x"


async def test_socket_herdr_merges_per_workspace_worktrees():
    from herdeck.bridge import SocketHerdr

    herdr = SocketHerdr("/nonexistent")
    calls = []

    async def fake_rpc(method, params, *, retry=True):
        calls.append((method, params))
        ws = params.get("workspace_id")
        # the same repo open in two workspaces returns the same worktree rows
        shared = {"path": "/r/a", "open_workspace_id": "wA", "branch": "main"}
        per_ws = {
            "wA": [shared],
            "wB": [shared, {"path": "/r/b", "open_workspace_id": "wB", "branch": "dev"}],
        }
        return {"result": {"worktrees": per_ws.get(ws, [])}}

    herdr._rpc = fake_rpc
    merged = await herdr.worktrees(["wA", "wB"])
    assert calls == [
        ("worktree.list", {"workspace_id": "wA"}),
        ("worktree.list", {"workspace_id": "wB"}),
    ]
    assert len(merged) == 2  # the shared row is de-duplicated
    assert {w["branch"] for w in merged} == {"main", "dev"}


# --- session.snapshot client method ---


async def test_socket_herdr_snapshot_returns_snapshot():
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")
    calls = []

    async def fake_rpc(method, params, *, retry=True):
        calls.append((method, params))
        return {"result": {"snapshot": {"agents": [], "workspaces": [], "tabs": []}}}

    h._rpc = fake_rpc
    snap = await h.snapshot()
    assert calls == [("session.snapshot", {})]
    assert snap == {"agents": [], "workspaces": [], "tabs": []}


@pytest.mark.parametrize(
    ("protocol", "expected_calls"),
    [
        (
            16,
            [
                ("session.snapshot", {}, True),
                ("agent.send", {"target": "w1:p1", "text": "continue"}, False),
                ("pane.send_keys", {"pane_id": "w1:p1", "keys": ["enter"]}, False),
            ],
        ),
        (
            17,
            [
                ("session.snapshot", {}, True),
                ("agent.prompt", {"target": "w1:p1", "text": "continue"}, False),
            ],
        ),
    ],
)
async def test_socket_herdr_send_text_uses_protocol_specific_agent_api(protocol, expected_calls):
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")
    calls = []

    async def fake_rpc(method, params, *, retry=True):
        calls.append((method, params, retry))
        if method == "session.snapshot":
            return {"result": {"snapshot": {"agents": [], "protocol": protocol}}}
        return {"result": {}}

    h._rpc = fake_rpc
    await h.send_text("w1:p1", "continue")

    assert calls == expected_calls


async def test_socket_herdr_answer_blocked_sends_text_then_enter():
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")
    calls = []

    async def fake_rpc(method, params, *, retry=True):
        calls.append((method, params, retry))
        return {"result": {}}

    h._rpc = fake_rpc
    await h.answer_blocked("w1:p1", "yes")

    assert calls == [
        ("pane.send_text", {"pane_id": "w1:p1", "text": "yes"}, False),
        ("pane.send_keys", {"pane_id": "w1:p1", "keys": ["enter"]}, False),
    ]


async def test_socket_herdr_answer_blocked_rejects_multiline_text():
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")
    calls = []

    async def fake_rpc(method, params, *, retry=True):
        calls.append((method, params, retry))
        return {"result": {}}

    h._rpc = fake_rpc
    with pytest.raises(ValueError, match="untypeable_blocked_answer"):
        await h.answer_blocked("w1:p1", "line one\nline two")

    assert calls == []  # rejected before any RPC


async def test_socket_herdr_answer_blocked_rejects_empty_answer():
    """An empty normalised answer must be refused, not typed: answer_blocked
    types text then presses enter, so typing nothing would silently confirm
    the blocked dialog's default choice."""
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")
    calls = []

    async def fake_rpc(method, params, *, retry=True):
        calls.append((method, params, retry))
        return {"result": {}}

    h._rpc = fake_rpc
    for whitespace_only in ("\n", "\r\n\r\n", "   "):
        with pytest.raises(ValueError, match="empty_blocked_answer"):
            await h.answer_blocked("w1:p1", whitespace_only)

    assert calls == []  # rejected before any RPC


async def test_socket_herdr_answer_blocked_rejects_invisible_answer():
    """The backstop matters on its own: choose_if_blocked calls answer_blocked
    directly, never through handle_client_message's send_text branch. A bare
    zero-width character types nothing, so the enter after it would confirm
    the dialog's default."""
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")
    calls = []

    async def fake_rpc(method, params, *, retry=True):
        calls.append((method, params, retry))
        return {"result": {}}

    h._rpc = fake_rpc
    illegible = ("\u200b", "\ufeff", "\u2060", "\u00ad", "\u180e", "\u0301", " \u200b\n", "\ue000")
    for invisible in illegible:
        with pytest.raises(ValueError, match="empty_blocked_answer"):
            await h.answer_blocked("w1:p1", invisible)

    assert calls == []  # rejected before any RPC


async def test_socket_herdr_answer_blocked_trims_edge_invisibles():
    """The backstop trims the same edges the handler does: choose_if_blocked
    passes its choice key straight in, so a BOM-prefixed "1" would otherwise be
    typed whole and leave the enter on the dialog's default."""
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")
    calls = []

    async def fake_rpc(method, params, *, retry=True):
        calls.append((method, params, retry))
        return {"result": {}}

    h._rpc = fake_rpc
    for raw in ("\ufeff1", "1\u200b", "1\u200b\n", "  1  ", "1\ufe0f\u20e3"):
        await h.answer_blocked("w1:p1", raw)

    assert calls == [
        ("pane.send_text", {"pane_id": "w1:p1", "text": "1"}, False),
        ("pane.send_keys", {"pane_id": "w1:p1", "keys": ["enter"]}, False),
    ] * 5


async def test_socket_herdr_answer_blocked_rejects_embedded_untypeable_char():
    """Line separators and controls are keystrokes the terminal acts on, not
    literal answer text — at the backstop as much as at the handler."""
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")
    calls = []

    async def fake_rpc(method, params, *, retry=True):
        calls.append((method, params, retry))
        return {"result": {}}

    h._rpc = fake_rpc
    for text in ("yes\u2028no", "yes\u2029no", "yes\u0085no", "yes\u007fno", "yes\tno", "yes\ud800"):
        with pytest.raises(ValueError, match="untypeable_blocked_answer"):
            await h.answer_blocked("w1:p1", text)

    assert calls == []  # rejected before any RPC


async def test_socket_herdr_answer_blocked_trims_trailing_newline():
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")
    calls = []

    async def fake_rpc(method, params, *, retry=True):
        calls.append((method, params, retry))
        return {"result": {}}

    h._rpc = fake_rpc
    # "yes\n  " and "yes " only survive because the trim is rstrip(), not
    # rstrip("\r\n") — and what gets typed is the normalised text.
    for raw in ("yes\r\n", "yes\n  ", "yes "):
        await h.answer_blocked("w1:p1", raw)

    assert calls == [
        ("pane.send_text", {"pane_id": "w1:p1", "text": "yes"}, False),
        ("pane.send_keys", {"pane_id": "w1:p1", "keys": ["enter"]}, False),
    ] * 3


async def test_socket_herdr_send_text_maps_stalled_prompt_to_actionable_error(caplog):
    import logging

    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")

    async def fake_rpc(method, params, *, retry=True):
        if method == "session.snapshot":
            return {"result": {"snapshot": {"agents": [], "protocol": 17}}}
        raise HerdrRpcError("agent.prompt", "agent_prompt_stalled", "herdr says: no reply")

    h._rpc = fake_rpc

    with caplog.at_level(logging.WARNING, logger="herdeck.bridge"):
        with pytest.raises(HerdrRpcError) as exc_info:
            await h.send_text("w1:p1", "continue")

    exc = exc_info.value
    assert exc.code == "agent_prompt_stalled"
    assert exc.message == (
        "herdr saw no state change within 5s after the prompt (agent_prompt_stalled)"
    )
    assert "w1:p1" in caplog.text
    assert "herdr says: no reply" in caplog.text  # herdr's own message, not just via __cause__


async def test_socket_herdr_send_text_propagates_unrelated_error_code():
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")

    async def fake_rpc(method, params, *, retry=True):
        if method == "session.snapshot":
            return {"result": {"snapshot": {"agents": [], "protocol": 17}}}
        raise HerdrRpcError("agent.prompt", "some_other_error", "boom")

    h._rpc = fake_rpc

    with pytest.raises(HerdrRpcError) as exc_info:
        await h.send_text("w1:p1", "continue")

    assert exc_info.value.code == "some_other_error"
    assert exc_info.value.message == "boom"


@pytest.mark.parametrize(
    ("argv", "expected_kind"),
    [
        (["codex", "-m", "gpt-5.4"], "codex"),
        (["qwen", "-m", "qwen-max"], "qwen"),
    ],
)
async def test_socket_herdr_start_agent_uses_managed_protocol_17_flow(argv, expected_kind):
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")
    calls = []

    async def fake_rpc(method, params, *, retry=True):
        calls.append((method, params, retry))
        if method == "session.snapshot":
            return {"result": {"snapshot": {"agents": [], "protocol": 17}}}
        if method == "tab.create":
            return {
                "result": {
                    "tab": {"tab_id": "w1:t2"},
                    "root_pane": {"pane_id": "w1:p2"},
                }
            }
        return {"result": {}}

    h._rpc = fake_rpc
    await h.start_agent("reviewer", argv)

    assert calls[:2] == [
        ("session.snapshot", {}, True),
        ("tab.create", {"focus": False, "label": "reviewer"}, False),
    ]
    method, params, retry = calls[2]
    assert method == "agent.start"
    assert retry is False
    assert params["kind"] == expected_kind
    assert params["pane_id"] == "w1:p2"
    assert params["args"] == argv[1:]
    assert params["name"].startswith(f"{expected_kind}-")


async def test_socket_herdr_start_agent_falls_back_to_raw_pane_on_unsupported_kind(caplog):
    """herdr rejects a kind it doesn't know yet (older herdr, newer herdeck
    _MANAGED_AGENT_KINDS entry) with `unsupported_agent_kind` before ever
    touching the pane, so the already-created tab is reused for raw pane
    input instead of being torn down. The degradation is logged so a user
    whose qwen agent silently loses managed startup has a diagnosable trace."""
    import logging

    from herdeck.bridge import HerdrRpcError, SocketHerdr

    h = SocketHerdr("/nonexistent")
    calls = []

    async def fake_rpc(method, params, *, retry=True):
        calls.append((method, params, retry))
        if method == "session.snapshot":
            return {"result": {"snapshot": {"agents": [], "protocol": 17}}}
        if method == "tab.create":
            return {
                "result": {
                    "tab": {"tab_id": "w1:t2"},
                    "root_pane": {"pane_id": "w1:p2"},
                }
            }
        if method == "agent.start":
            raise HerdrRpcError("agent.start", "unsupported_agent_kind", "unsupported kind qwen")
        return {"result": {}}

    h._rpc = fake_rpc
    with caplog.at_level(logging.WARNING, logger="herdeck.bridge"):
        await h.start_agent("reviewer", ["qwen", "-m", "qwen-max"])

    assert "qwen" in caplog.text
    assert "w1:p2" in caplog.text

    methods = [call[0] for call in calls]
    assert methods == [
        "session.snapshot",
        "tab.create",
        "agent.start",
        "pane.send_text",
        "pane.send_keys",
    ]
    assert "tab.close" not in methods
    send_text_call = calls[3]
    assert send_text_call[1] == {"pane_id": "w1:p2", "text": "qwen -m qwen-max"}
    send_keys_call = calls[4]
    assert send_keys_call[1] == {"pane_id": "w1:p2", "keys": ["enter"]}


async def test_socket_herdr_start_agent_closes_tab_on_other_agent_start_error():
    """Only `unsupported_agent_kind` gets the raw-pane fallback; any other
    agent.start failure (e.g. herdr not finding the just-created pane) must
    keep closing the tab and re-raising, exactly as before."""
    from herdeck.bridge import HerdrRpcError, SocketHerdr

    h = SocketHerdr("/nonexistent")
    calls = []

    async def fake_rpc(method, params, *, retry=True):
        calls.append((method, params, retry))
        if method == "session.snapshot":
            return {"result": {"snapshot": {"agents": [], "protocol": 17}}}
        if method == "tab.create":
            return {
                "result": {
                    "tab": {"tab_id": "w1:t2"},
                    "root_pane": {"pane_id": "w1:p2"},
                }
            }
        if method == "agent.start":
            raise HerdrRpcError("agent.start", "agent_pane_not_found", "agent target pane not found")
        return {"result": {}}

    h._rpc = fake_rpc

    with pytest.raises(HerdrRpcError) as exc_info:
        await h.start_agent("reviewer", ["qwen", "-m", "qwen-max"])

    assert exc_info.value.code == "agent_pane_not_found"
    methods = [call[0] for call in calls]
    assert "pane.send_text" not in methods
    assert calls[-1] == ("tab.close", {"tab_id": "w1:t2"}, False)


async def test_socket_herdr_start_agent_preserves_custom_command_on_protocol_17():
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")
    calls = []

    async def fake_rpc(method, params, *, retry=True):
        calls.append((method, params, retry))
        if method == "session.snapshot":
            return {"result": {"snapshot": {"agents": [], "protocol": 17}}}
        if method == "tab.create":
            return {
                "result": {
                    "tab": {"tab_id": "w1:t2"},
                    "root_pane": {"pane_id": "w1:p2"},
                }
            }
        return {"result": {}}

    h._rpc = fake_rpc
    await h.start_agent("custom", ["agent-wrapper", "--label", "two words"])

    assert calls == [
        ("session.snapshot", {}, True),
        ("tab.create", {"focus": False, "label": "custom"}, False),
        (
            "pane.send_text",
            {"pane_id": "w1:p2", "text": "agent-wrapper --label 'two words'"},
            False,
        ),
        ("pane.send_keys", {"pane_id": "w1:p2", "keys": ["enter"]}, False),
    ]


async def test_socket_herdr_start_agent_preserves_explicit_executable_path():
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")
    calls = []

    async def fake_rpc(method, params, *, retry=True):
        calls.append((method, params, retry))
        if method == "session.snapshot":
            return {"result": {"snapshot": {"agents": [], "protocol": 17}}}
        if method == "tab.create":
            return {
                "result": {
                    "tab": {"tab_id": "w1:t2"},
                    "root_pane": {"pane_id": "w1:p2"},
                }
            }
        return {"result": {}}

    h._rpc = fake_rpc
    await h.start_agent("codex", ["/opt/homebrew/bin/codex", "--quiet"])

    assert calls[2:] == [
        (
            "pane.send_text",
            {"pane_id": "w1:p2", "text": "/opt/homebrew/bin/codex --quiet"},
            False,
        ),
        ("pane.send_keys", {"pane_id": "w1:p2", "keys": ["enter"]}, False),
    ]


async def test_socket_herdr_start_agent_closes_created_tab_when_launch_fails():
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")
    calls = []

    async def fake_rpc(method, params, *, retry=True):
        calls.append((method, params, retry))
        if method == "session.snapshot":
            return {"result": {"snapshot": {"agents": [], "protocol": 17}}}
        if method == "tab.create":
            return {
                "result": {
                    "tab": {"tab_id": "w1:t2"},
                    "root_pane": {"pane_id": "w1:p2"},
                }
            }
        if method == "agent.start":
            raise RuntimeError("agent failed to start")
        return {"result": {}}

    h._rpc = fake_rpc

    with pytest.raises(RuntimeError, match="agent failed to start"):
        await h.start_agent("codex", ["codex"])

    assert calls[-1] == ("tab.close", {"tab_id": "w1:t2"}, False)


async def test_socket_herdr_start_agent_closes_created_tab_when_cancelled():
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")
    calls = []
    launch_started = asyncio.Event()

    async def fake_rpc(method, params, *, retry=True):
        calls.append((method, params, retry))
        if method == "session.snapshot":
            return {"result": {"snapshot": {"agents": [], "protocol": 17}}}
        if method == "tab.create":
            return {
                "result": {
                    "tab": {"tab_id": "w1:t2"},
                    "root_pane": {"pane_id": "w1:p2"},
                }
            }
        if method == "agent.start":
            launch_started.set()
            await asyncio.Event().wait()
        return {"result": {}}

    h._rpc = fake_rpc
    task = asyncio.create_task(h.start_agent("codex", ["codex"]))
    await asyncio.wait_for(launch_started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls[-1] == ("tab.close", {"tab_id": "w1:t2"}, False)


async def test_socket_herdr_start_agent_closes_malformed_created_tab():
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")
    calls = []

    async def fake_rpc(method, params, *, retry=True):
        calls.append((method, params, retry))
        if method == "session.snapshot":
            return {"result": {"snapshot": {"agents": [], "protocol": 17}}}
        if method == "tab.create":
            return {"result": {"tab": {"tab_id": "w1:t2"}}}
        return {"result": {}}

    h._rpc = fake_rpc

    with pytest.raises(RuntimeError, match="malformed tab or root pane"):
        await h.start_agent("codex", ["codex"])

    assert calls[-1] == ("tab.close", {"tab_id": "w1:t2"}, False)


async def test_socket_herdr_start_agent_keeps_protocol_16_contract():
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")
    calls = []

    async def fake_rpc(method, params, *, retry=True):
        calls.append((method, params, retry))
        if method == "session.snapshot":
            return {"result": {"snapshot": {"agents": [], "protocol": 16}}}
        return {"result": {}}

    h._rpc = fake_rpc
    await h.start_agent("claude", ["claude"])

    assert calls == [
        ("session.snapshot", {}, True),
        ("agent.start", {"name": "claude", "argv": ["claude"]}, False),
    ]


async def test_socket_herdr_start_agent_keeps_empty_protocol_16_arguments():
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")
    calls = []

    async def fake_rpc(method, params, *, retry=True):
        calls.append((method, params, retry))
        if method == "session.snapshot":
            return {"result": {"snapshot": {"agents": [], "protocol": 16}}}
        return {"result": {}}

    h._rpc = fake_rpc
    await h.start_agent("claude", ["claude", ""])

    assert calls[-1] == (
        "agent.start",
        {"name": "claude", "argv": ["claude", ""]},
        False,
    )


async def test_socket_herdr_snapshot_rejects_malformed_tokens():
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")

    async def fake_rpc(method, params, *, retry=True):
        return {
            "result": {
                "snapshot": {
                    "agents": [
                        {
                            "pane_id": "w1:p1",
                            "terminal_id": "term-1",
                            "tokens": {"work_item": 123},
                        }
                    ]
                }
            }
        }

    h._rpc = fake_rpc

    with pytest.raises(RuntimeError, match="token values must be strings"):
        await h.snapshot()


async def test_socket_herdr_snapshot_tolerates_malformed_state_labels():
    # Any integration may write state_labels, and a snapshot exception degrades
    # to a deck frozen on its last frame -- a cosmetic label must never do that.
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")

    async def fake_rpc(method, params, *, retry=True):
        return {
            "result": {
                "snapshot": {
                    "agents": [
                        {
                            "pane_id": "w1:p1",
                            "state_labels": {"working": 123},
                        }
                    ]
                }
            }
        }

    h._rpc = fake_rpc

    snapshot = await h.snapshot()

    assert _herdr_pane_to_wire(snapshot["agents"][0])["state_labels"] == {}


async def test_socket_herdr_snapshot_accepts_absent_state_labels():
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")

    async def fake_rpc(method, params, *, retry=True):
        return {"result": {"snapshot": {"agents": [{"pane_id": "w1:p1"}]}}}

    h._rpc = fake_rpc

    assert (await h.snapshot())["agents"] == [{"pane_id": "w1:p1"}]


async def test_socket_herdr_snapshot_maps_unknown_method_to_version_hint():
    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")

    async def fake_rpc(method, params, *, retry=True):
        raise RuntimeError(
            "herdr RPC session.snapshot failed: invalid request: "
            "unknown variant `session.snapshot`, expected one of `ping`, `server.stop`"
        )

    h._rpc = fake_rpc
    with pytest.raises(RuntimeError, match="herdr >= 0.7.4"):
        await h.snapshot()


async def test_socket_herdr_read_pane_warns_on_truncated_response(caplog):
    import logging

    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")

    async def fake_rpc(method, params, *, retry=True):
        return {"result": {"read": {"text": "clipped prompt", "truncated": True}}}

    h._rpc = fake_rpc

    with caplog.at_level(logging.WARNING, logger="herdeck.bridge"):
        text = await h.read_pane("w1:p1", "detection")

    assert text == "clipped prompt"
    assert "w1:p1" in caplog.text
    assert "truncated" in caplog.text


async def test_socket_herdr_read_pane_no_warning_when_not_truncated(caplog):
    import logging

    from herdeck.bridge import SocketHerdr

    h = SocketHerdr("/nonexistent")

    async def fake_rpc(method, params, *, retry=True):
        return {"result": {"read": {"text": "full prompt", "truncated": False}}}

    h._rpc = fake_rpc

    with caplog.at_level(logging.WARNING, logger="herdeck.bridge"):
        text = await h.read_pane("w1:p1", "detection")

    assert text == "full prompt"
    assert caplog.text == ""


async def test_stub_herdr_snapshot_composes_lists():
    stub = StubHerdr(
        panes=[raw_pane()],
        workspaces=[{"workspace_id": "w1", "label": "api"}],
        tabs=[{"tab_id": "w1:t1", "label": "1"}],
    )
    snap = await stub.snapshot()
    assert snap["agents"][0]["pane_id"] == "w1:p1"
    assert snap["workspaces"] == [{"workspace_id": "w1", "label": "api"}]
    assert snap["tabs"] == [{"tab_id": "w1:t1", "label": "1"}]


# --- push-event digestion (labels ride the snapshot; worktrees need staling) ---


def test_note_event_stales_worktrees_and_flags_fleet_changes():
    from herdeck.bridge import HerdrEvents

    ev = HerdrEvents(StubHerdr(panes=[]), poll_interval=100)
    ev._worktrees_stale = False
    # label renames ride the next snapshot: wake-only, no staling, no re-subscribe
    assert ev._note_event("tab_renamed") is False
    assert ev._note_event("workspace_renamed") is False
    assert ev._note_event("workspace_updated") is False
    assert ev._worktrees_stale is False
    # worktree membership changed: branch labels must refetch
    assert ev._note_event("worktree_opened") is False
    assert ev._worktrees_stale is True
    ev._worktrees_stale = False
    assert ev._note_event("worktree_removed") is False
    assert ev._worktrees_stale is True
    # fleet change: re-subscribe per-pane status subs AND refetch worktrees
    ev._worktrees_stale = False
    assert ev._note_event("pane_created") is True
    assert ev._worktrees_stale is True
    # unknown/None events are inert
    ev._worktrees_stale = False
    assert ev._note_event(None) is False
    assert ev._worktrees_stale is False


async def test_tab_rename_wake_delivers_fresh_labels_without_worktree_refetch():
    import asyncio

    from herdeck.bridge import HerdrEvents

    pane = raw_pane("w1:p1", agent="claude", status="idle")
    pane["tab_id"] = "w1:t1"
    stub = StubHerdr(panes=[pane], tabs=[{"tab_id": "w1:t1", "label": "1"}])
    ev = HerdrEvents(stub, poll_interval=100)
    gen = ev.stream()
    first = await gen.__anext__()
    assert first[0]["tab"] == "1"
    stub._tabs = [{"tab_id": "w1:t1", "label": "review"}]  # herdr-side rename
    ev._wake.set()  # what a tab.renamed push event does
    second = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert second[0]["tab"] == "review"
    assert stub.worktree_queries == [["w1"]]  # worktrees were NOT refetched
    await gen.aclose()


def test_listen_subscribes_to_label_and_worktree_events():
    from herdeck.bridge import _GLOBAL_EVENT_TYPES, _LABEL_EVENT_TYPES

    assert set(_LABEL_EVENT_TYPES) == {
        "pane.updated",
        "tab.renamed",
        "workspace.renamed",
        "workspace.updated",
        "workspace.metadata_updated",
        "worktree.created",
        "worktree.opened",
        "worktree.removed",
    }
    assert not set(_LABEL_EVENT_TYPES) & set(_GLOBAL_EVENT_TYPES)
    assert set(_GLOBAL_EVENT_TYPES) == {
        "pane.created",
        "pane.closed",
        "pane.exited",
        "pane.moved",
        "workspace.closed",
        "tab.closed",
    }
    assert "pane.agent_detected" not in _GLOBAL_EVENT_TYPES


def test_event_subscriptions_cover_all_snapshot_panes_not_only_agents():
    from herdeck.bridge import HerdrEvents

    snapshot = {
        "agents": [raw_pane("w1:p1")],
        "panes": [
            {"pane_id": "w1:p1"},
            {"pane_id": "w1:p2"},
        ],
    }

    pane_ids = HerdrEvents._snapshot_pane_ids(snapshot)
    subscriptions = HerdrEvents._subscriptions_for(pane_ids)

    status_ids = {
        sub["pane_id"] for sub in subscriptions if sub.get("type") == "pane.agent_status_changed"
    }
    assert status_ids == {"w1:p1", "w1:p2"}


@pytest.mark.asyncio
async def test_retained_lifecycle_event_only_resubscribes_after_real_topology_change():
    from herdeck.bridge import HerdrEvents

    class SnapshotHerdr:
        def __init__(self):
            self.snapshot_value = {
                "agents": [raw_pane("w1:p1")],
                "panes": [{"pane_id": "w1:p1"}, {"pane_id": "w1:p2"}],
            }

        async def snapshot(self):
            return self.snapshot_value

    herdr = SnapshotHerdr()
    events = HerdrEvents(herdr)
    subscribed = {"w1:p1", "w1:p2"}

    assert await events._topology_changed("pane_created", subscribed) is False

    herdr.snapshot_value = {
        "agents": [raw_pane("w1:p1")],
        "panes": [{"pane_id": "w1:p1"}, {"pane_id": "w1:p3"}],
    }
    assert await events._topology_changed("pane_moved", subscribed) is True


# --- herdr version gate (hard requirement: >= 0.7.4) ---


async def test_require_snapshot_support_rejects_old_herdr():
    from herdeck.bridge import _SNAPSHOT_UNSUPPORTED, _require_snapshot_support

    class OldHerdr:
        async def snapshot(self):
            raise RuntimeError(_SNAPSHOT_UNSUPPORTED)

    with pytest.raises(RuntimeError, match="herdr >= 0.7.4"):
        await _require_snapshot_support(OldHerdr())


async def test_require_snapshot_support_tolerates_down_or_flaky_herdr():
    from herdeck.bridge import _require_snapshot_support

    class DownHerdr:
        async def snapshot(self):
            raise ConnectionError("socket not there yet")

    class FlakyHerdr:
        async def snapshot(self):
            raise RuntimeError("herdr RPC session.snapshot failed: live update in progress")

    await _require_snapshot_support(DownHerdr())  # must not raise
    await _require_snapshot_support(FlakyHerdr())  # must not raise


@pytest.mark.parametrize(
    ("version", "expect_warning"),
    [
        ("0.7.3", True),  # below the floor
        ("0.7.4", False),  # exactly at the floor: must not warn
        ("0.8.2", False),  # above the floor
    ],
)
async def test_require_snapshot_support_warns_only_below_recommended_version(
    caplog, version, expect_warning
):
    import logging

    from herdeck.bridge import _require_snapshot_support

    class VersionedHerdr:
        async def snapshot(self):
            return {"agents": [], "protocol": 18, "version": version}

    with caplog.at_level(logging.WARNING, logger="herdeck.bridge"):
        await _require_snapshot_support(VersionedHerdr())  # must not raise

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    if expect_warning:
        assert len(warnings) == 1
        assert version in caplog.text
        assert "0.7.4" in caplog.text
    else:
        assert warnings == []


async def test_require_snapshot_support_ignores_missing_or_malformed_version(caplog):
    import logging

    from herdeck.bridge import _require_snapshot_support

    class NoVersionHerdr:
        async def snapshot(self):
            return {"agents": [], "protocol": 20}

    class WeirdVersionHerdr:
        async def snapshot(self):
            return {"agents": [], "protocol": 20, "version": "not-a-version"}

    class NoneSnapshotHerdr:
        # A non-conforming HerdrClient (stub, future implementation) that
        # returns something other than a dict must not turn this advisory
        # probe fatal.
        async def snapshot(self):
            return None

    class ListSnapshotHerdr:
        async def snapshot(self):
            return []

    with caplog.at_level(logging.WARNING, logger="herdeck.bridge"):
        await _require_snapshot_support(NoVersionHerdr())
        await _require_snapshot_support(WeirdVersionHerdr())
        await _require_snapshot_support(NoneSnapshotHerdr())  # must not raise
        await _require_snapshot_support(ListSnapshotHerdr())  # must not raise

    assert caplog.text == ""


async def test_start_local_bridge_probes_snapshot_support():
    from herdeck.bridge import _SNAPSHOT_UNSUPPORTED, start_local_bridge

    class OldHerdr:
        async def snapshot(self):
            raise RuntimeError(_SNAPSHOT_UNSUPPORTED)

    with pytest.raises(RuntimeError, match="herdr >= 0.7.4"):
        await start_local_bridge("/unused.sock", herdr=OldHerdr())


async def test_require_snapshot_support_times_out_instead_of_hanging(monkeypatch):
    import asyncio

    from herdeck import bridge as bridge_mod
    from herdeck.bridge import _require_snapshot_support

    monkeypatch.setattr(bridge_mod, "_PROBE_TIMEOUT", 0.05)

    class HangingHerdr:
        async def snapshot(self):
            await asyncio.Event().wait()  # accepts but never answers

    await asyncio.wait_for(
        _require_snapshot_support(HangingHerdr()), timeout=1.0
    )  # must return, not raise


async def test_stream_surfaces_version_error_instead_of_retrying():
    from herdeck.bridge import _SNAPSHOT_UNSUPPORTED, HerdrEvents

    class OldHerdr:
        async def snapshot(self):
            raise RuntimeError(_SNAPSHOT_UNSUPPORTED)

    gen = HerdrEvents(OldHerdr(), poll_interval=0).stream()
    with pytest.raises(RuntimeError, match="herdr >= 0.7.4"):
        await gen.__anext__()


async def test_start_local_bridge_logs_when_broadcast_dies_after_startup(caplog):
    """The startup probe tolerates herdr being transiently down; if an old
    herdr answers once the bridge is already serving, the detached broadcast
    task (unlike serve()'s foreground _broadcast) has no caller to surface
    the failure to — it must at least log loudly instead of dying silently."""
    import asyncio
    import logging

    from herdeck.bridge import _SNAPSHOT_UNSUPPORTED, start_local_bridge

    calls = {"n": 0}

    class TransientlyDownThenOldHerdr:
        async def snapshot(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("herdr not up yet")  # tolerated by the probe
            raise RuntimeError(_SNAPSHOT_UNSUPPORTED)  # first real stream snapshot

    host, port, token, (server, btask) = await start_local_bridge(
        "/unused.sock", herdr=TransientlyDownThenOldHerdr()
    )
    try:
        with caplog.at_level(logging.ERROR, logger="herdeck.bridge"):
            with pytest.raises(RuntimeError, match="herdr >= 0.7.4"):
                await asyncio.wait_for(btask, timeout=1.0)
        assert "broadcast stopped" in caplog.text
    finally:
        server.close()
        await server.wait_closed()


def test_note_event_ignores_global_agent_detection_feed():
    from herdeck.bridge import HerdrEvents

    ev = HerdrEvents(StubHerdr(panes=[]), poll_interval=100)
    ev._worktrees_stale = False
    # Herdr 0.7.3 replays pane.agent_detected aggressively. Discovery now rides
    # per-pane status subscriptions for every snapshot pane, so this global
    # event is never a reason to rebuild the stream.
    assert ev._note_event("pane_agent_detected", "w1:p1", {"w1:p1"}) is False
    assert ev._worktrees_stale is False
    assert ev._note_event("pane_agent_detected", "w9:p1", {"w1:p1"}) is False
    assert ev._worktrees_stale is False


# --- live terminal observe -------------------------------------------------

_FAKE_OBSERVE_BODY = r"""import base64
import json
import os
import sys
import time

args = sys.argv[1:]
pane = args[3]
cols = int(args[args.index("--cols") + 1])
rows = int(args[args.index("--rows") + 1])
pidfile = os.environ.get("FAKE_OBSERVE_PIDFILE")
if pidfile:
    with open(pidfile, "w") as fh:
        fh.write(str(os.getpid()))
starts = os.environ.get("FAKE_OBSERVE_STARTS")
if starts:
    with open(starts, "a") as fh:
        fh.write(pane + "\n")
socketfile = os.environ.get("FAKE_OBSERVE_SOCKETFILE")
if socketfile:
    with open(socketfile, "w") as fh:
        fh.write(os.environ.get("HERDR_SOCKET", ""))
if pane == "w9:gone":
    print(json.dumps({"type": "terminal.closed", "reason": "terminal target not found"}), flush=True)
    raise SystemExit(0)
if pane == "w9:dead":
    raise SystemExit(1)
if pane == "w9:badframe":
    print(json.dumps({"type": "terminal.frame", "seq": "bad", "full": True,
                      "width": cols, "height": rows, "encoding": "ansi", "bytes": "QQ=="}), flush=True)
    time.sleep(60)
big = base64.b64encode(b"x" * 100_000).decode()
print(json.dumps({"type": "terminal.frame", "seq": 1, "full": True,
                  "width": cols, "height": rows, "encoding": "ansi", "bytes": big}), flush=True)
print(json.dumps({"type": "terminal.frame", "seq": 2, "full": False,
                  "width": cols, "height": rows, "encoding": "ansi", "bytes": "aGk="}), flush=True)
if pane == "w9:short":
    raise SystemExit(0)
time.sleep(60)
"""


@pytest.fixture
def fake_observe_bin(tmp_path, monkeypatch):
    from herdeck import bridge as bridge_mod

    script = tmp_path / "fake-herdr"
    script.write_text(f"#!{sys.executable}\n{_FAKE_OBSERVE_BODY}")
    script.chmod(0o755)
    monkeypatch.setenv("HERDECK_HERDR_BIN", str(script))
    monkeypatch.setattr(bridge_mod, "_observe_total", 0, raising=False)
    return script


async def _open_bridge_client(socket_path="/unused.sock"):
    import websockets

    from herdeck.bridge import StubHerdr, start_local_bridge

    host, port, token, runtime = await start_local_bridge(socket_path, herdr=StubHerdr(panes=[]))
    server, btask = runtime
    ws = await websockets.connect(
        f"ws://{host}:{port}",
        additional_headers={"Authorization": f"Bearer {token}"},
    )
    assert json.loads(await ws.recv())["type"] == "snapshot"
    return ws, server, btask


async def _close_bridge_client(ws, server, btask):
    await ws.close()
    btask.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await btask
    server.close()
    await server.wait_closed()


async def _recv_term(ws, kind, req=None, timeout=5.0):
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if msg.get("type") == kind and (req is None or msg.get("req") == req):
            return msg
        assert msg.get("type") in {"snapshot", "term_frame", "term_closed"}, msg


async def test_observe_streams_large_frames_and_stop_closes(fake_observe_bin):
    ws, server, btask = await _open_bridge_client()
    try:
        await ws.send(
            json.dumps(
                {
                    "type": "observe",
                    "req": "t1",
                    "pane_id": "w1:p1",
                    "cols": 100,
                    "rows": 30,
                }
            )
        )
        first = await _recv_term(ws, "term_frame", "t1")
        assert first["seq"] == 1 and first["full"] is True
        assert first["cols"] == 100 and first["rows"] == 30
        assert len(first["data"]) > 100_000
        second = await _recv_term(ws, "term_frame", "t1")
        assert second["seq"] == 2 and second["data"] == "aGk="

        await ws.send(json.dumps({"type": "observe_stop", "req": "t1"}))
        closed = await _recv_term(ws, "term_closed", "t1")
        assert closed["reason"]
    finally:
        await _close_bridge_client(ws, server, btask)


def test_resolve_herdr_bin_checks_homebrew_and_cargo_fallbacks(monkeypatch):
    from herdeck import bridge as bridge_mod

    monkeypatch.delenv("HERDECK_HERDR_BIN", raising=False)
    monkeypatch.setattr(bridge_mod.shutil, "which", lambda name: None)
    wanted = "/opt/homebrew/bin/herdr"
    monkeypatch.setattr(bridge_mod.os, "access", lambda path, mode: path == wanted)
    assert bridge_mod._resolve_herdr_bin() == wanted


async def test_observe_uses_the_bridge_socket_path(fake_observe_bin, tmp_path, monkeypatch):
    socketfile = tmp_path / "socket-path"
    monkeypatch.delenv("HERDR_SOCKET", raising=False)
    monkeypatch.setenv("FAKE_OBSERVE_SOCKETFILE", str(socketfile))
    ws, server, btask = await _open_bridge_client("/tmp/herdr-preview.sock")
    try:
        await ws.send(
            json.dumps(
                {
                    "type": "observe",
                    "req": "socket",
                    "pane_id": "w1:p1",
                    "cols": 80,
                    "rows": 24,
                }
            )
        )
        await _recv_term(ws, "term_frame", "socket")
        assert socketfile.read_text() == "/tmp/herdr-preview.sock"
    finally:
        await _close_bridge_client(ws, server, btask)


async def test_observe_immediate_stop_releases_global_slot(fake_observe_bin):
    from herdeck import bridge as bridge_mod

    ws, server, btask = await _open_bridge_client()
    try:
        await ws.send(
            json.dumps(
                {
                    "type": "observe",
                    "req": "instant",
                    "pane_id": "w1:p1",
                    "cols": 80,
                    "rows": 24,
                }
            )
        )
        await ws.send(json.dumps({"type": "observe_stop", "req": "instant"}))
        assert (await _recv_term(ws, "term_closed", "instant"))["reason"] == "stopped"
        for _ in range(20):
            if bridge_mod._observe_total == 0:
                break
            await asyncio.sleep(0)
        assert bridge_mod._observe_total == 0
    finally:
        await _close_bridge_client(ws, server, btask)


async def test_observe_passes_through_terminal_closed_reason(fake_observe_bin):
    ws, server, btask = await _open_bridge_client()
    try:
        await ws.send(
            json.dumps(
                {
                    "type": "observe",
                    "req": "gone",
                    "pane_id": "w9:gone",
                    "cols": 80,
                    "rows": 24,
                }
            )
        )
        closed = await _recv_term(ws, "term_closed", "gone")
        assert closed["reason"] == "terminal target not found"
    finally:
        await _close_bridge_client(ws, server, btask)


async def test_observe_no_output_hints_at_required_herdr_version(fake_observe_bin):
    ws, server, btask = await _open_bridge_client()
    try:
        await ws.send(
            json.dumps(
                {
                    "type": "observe",
                    "req": "dead",
                    "pane_id": "w9:dead",
                    "cols": 80,
                    "rows": 24,
                }
            )
        )
        closed = await _recv_term(ws, "term_closed", "dead")
        assert "herdr >= 0.7.3" in closed["reason"]
    finally:
        await _close_bridge_client(ws, server, btask)


async def test_observe_eof_without_closed_line_has_stable_reason(fake_observe_bin):
    ws, server, btask = await _open_bridge_client()
    try:
        await ws.send(
            json.dumps(
                {
                    "type": "observe",
                    "req": "short",
                    "pane_id": "w9:short",
                    "cols": 80,
                    "rows": 24,
                }
            )
        )
        await _recv_term(ws, "term_frame", "short")
        closed = await _recv_term(ws, "term_closed", "short")
        assert closed["reason"] == "stream ended"
    finally:
        await _close_bridge_client(ws, server, btask)


async def test_observe_invalid_frame_closes_only_that_stream(fake_observe_bin):
    ws, server, btask = await _open_bridge_client()
    try:
        await ws.send(
            json.dumps(
                {
                    "type": "observe",
                    "req": "bad",
                    "pane_id": "w9:badframe",
                    "cols": 80,
                    "rows": 24,
                }
            )
        )
        closed = await _recv_term(ws, "term_closed", "bad")
        assert closed["reason"] == "invalid terminal frame from herdr"

        await ws.send(
            json.dumps(
                {
                    "type": "observe",
                    "req": "good",
                    "pane_id": "w1:p1",
                    "cols": 80,
                    "rows": 24,
                }
            )
        )
        assert (await _recv_term(ws, "term_frame", "good"))["seq"] == 1
    finally:
        await _close_bridge_client(ws, server, btask)


async def test_observe_clamps_explicit_zero_and_large_dimensions(fake_observe_bin):
    ws, server, btask = await _open_bridge_client()
    try:
        await ws.send(
            json.dumps(
                {
                    "type": "observe",
                    "req": "clamp",
                    "pane_id": "w1:p1",
                    "cols": 9999,
                    "rows": 0,
                }
            )
        )
        frame = await _recv_term(ws, "term_frame", "clamp")
        assert frame["cols"] == 240 and frame["rows"] == 5
    finally:
        await _close_bridge_client(ws, server, btask)


async def test_observe_rejects_malformed_control_without_using_a_slot(fake_observe_bin):
    from herdeck import bridge as bridge_mod

    ws, server, btask = await _open_bridge_client()
    try:
        bad = [
            ({"type": "observe", "pane_id": "w1:p1"}, ""),
            ({"type": "observe", "req": "empty", "pane_id": ""}, "empty"),
            (
                {
                    "type": "observe",
                    "req": "dims",
                    "pane_id": "w1:p1",
                    "cols": True,
                },
                "dims",
            ),
        ]
        for payload, req in bad:
            await ws.send(json.dumps(payload))
            assert (await _recv_term(ws, "term_closed", req))["reason"]
        assert bridge_mod._observe_total == 0
    finally:
        await _close_bridge_client(ws, server, btask)


async def test_observe_overflowing_dimension_does_not_close_websocket(fake_observe_bin):
    ws, server, btask = await _open_bridge_client()
    try:
        await ws.send(
            '{"type":"observe","req":"overflow","pane_id":"w1:p1","cols":1e309,"rows":24}'
        )
        closed = await _recv_term(ws, "term_closed", "overflow")
        assert closed["reason"] == "invalid terminal dimensions"

        await ws.send(
            json.dumps(
                {
                    "type": "observe",
                    "req": "still-open",
                    "pane_id": "w1:p1",
                    "cols": 80,
                    "rows": 24,
                }
            )
        )
        await _recv_term(ws, "term_frame", "still-open")
    finally:
        await _close_bridge_client(ws, server, btask)


async def test_observe_duplicate_request_is_idempotent(fake_observe_bin, tmp_path, monkeypatch):
    starts = tmp_path / "starts"
    monkeypatch.setenv("FAKE_OBSERVE_STARTS", str(starts))
    ws, server, btask = await _open_bridge_client()
    try:
        request = {
            "type": "observe",
            "req": "same",
            "pane_id": "w1:p1",
            "cols": 80,
            "rows": 24,
        }
        await ws.send(json.dumps(request))
        await _recv_term(ws, "term_frame", "same")
        await ws.send(json.dumps({**request, "pane_id": "w9:gone"}))
        await asyncio.sleep(0.05)
        assert starts.read_text().splitlines() == ["w1:p1"]
        await ws.send(json.dumps({"type": "observe_stop", "req": "same"}))
        await _recv_term(ws, "term_closed", "same")
    finally:
        await _close_bridge_client(ws, server, btask)


async def test_observe_per_client_cap_releases_after_stop(fake_observe_bin):
    ws, server, btask = await _open_bridge_client()
    try:
        for number in range(3):
            req = f"cap{number}"
            await ws.send(
                json.dumps(
                    {
                        "type": "observe",
                        "req": req,
                        "pane_id": "w1:p1",
                        "cols": 80,
                        "rows": 24,
                    }
                )
            )
            await _recv_term(ws, "term_frame", req)

        await ws.send(
            json.dumps(
                {
                    "type": "observe",
                    "req": "rejected",
                    "pane_id": "w1:p1",
                    "cols": 80,
                    "rows": 24,
                }
            )
        )
        rejected = await _recv_term(ws, "term_closed", "rejected")
        assert rejected["reason"] == "too many live previews"

        await ws.send(json.dumps({"type": "observe_stop", "req": "cap0"}))
        await _recv_term(ws, "term_closed", "cap0")
        await ws.send(
            json.dumps(
                {
                    "type": "observe",
                    "req": "replacement",
                    "pane_id": "w1:p1",
                    "cols": 80,
                    "rows": 24,
                }
            )
        )
        await _recv_term(ws, "term_frame", "replacement")
    finally:
        await _close_bridge_client(ws, server, btask)


async def test_observe_global_cap_spans_clients_and_releases(fake_observe_bin):
    import websockets

    from herdeck.bridge import StubHerdr, start_local_bridge

    host, port, token, (server, btask) = await start_local_bridge(
        "/unused.sock", herdr=StubHerdr(panes=[])
    )
    clients = []
    try:
        for _ in range(3):
            ws = await websockets.connect(
                f"ws://{host}:{port}",
                additional_headers={"Authorization": f"Bearer {token}"},
            )
            assert json.loads(await ws.recv())["type"] == "snapshot"
            clients.append(ws)

        counts = (3, 3, 2)
        for client_no, count in enumerate(counts):
            for stream_no in range(count):
                req = f"g{client_no}-{stream_no}"
                await clients[client_no].send(
                    json.dumps(
                        {
                            "type": "observe",
                            "req": req,
                            "pane_id": "w1:p1",
                            "cols": 80,
                            "rows": 24,
                        }
                    )
                )
                await _recv_term(clients[client_no], "term_frame", req)

        await clients[2].send(
            json.dumps(
                {
                    "type": "observe",
                    "req": "global-reject",
                    "pane_id": "w1:p1",
                    "cols": 80,
                    "rows": 24,
                }
            )
        )
        rejected = await _recv_term(clients[2], "term_closed", "global-reject")
        assert rejected["reason"] == "too many live previews"

        await clients[0].send(json.dumps({"type": "observe_stop", "req": "g0-0"}))
        await _recv_term(clients[0], "term_closed", "g0-0")
        await clients[2].send(
            json.dumps(
                {
                    "type": "observe",
                    "req": "global-replacement",
                    "pane_id": "w1:p1",
                    "cols": 80,
                    "rows": 24,
                }
            )
        )
        await _recv_term(clients[2], "term_frame", "global-replacement")
    finally:
        await asyncio.gather(*(ws.close() for ws in clients), return_exceptions=True)
        btask.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await btask
        server.close()
        await server.wait_closed()


async def test_observe_spawn_failure_releases_global_slot(fake_observe_bin, monkeypatch):
    from herdeck import bridge as bridge_mod

    monkeypatch.setenv("HERDECK_HERDR_BIN", "/nonexistent/herdr")
    ws, server, btask = await _open_bridge_client()
    try:
        await ws.send(
            json.dumps(
                {
                    "type": "observe",
                    "req": "missing",
                    "pane_id": "w1:p1",
                    "cols": 80,
                    "rows": 24,
                }
            )
        )
        closed = await _recv_term(ws, "term_closed", "missing")
        assert "could not start herdr" in closed["reason"]
        for _ in range(20):
            if bridge_mod._observe_total == 0:
                break
            await asyncio.sleep(0)
        assert bridge_mod._observe_total == 0

        monkeypatch.setenv("HERDECK_HERDR_BIN", str(fake_observe_bin))
        await ws.send(
            json.dumps(
                {
                    "type": "observe",
                    "req": "after-missing",
                    "pane_id": "w1:p1",
                    "cols": 80,
                    "rows": 24,
                }
            )
        )
        await _recv_term(ws, "term_frame", "after-missing")
    finally:
        await _close_bridge_client(ws, server, btask)


async def test_observe_disconnect_reaps_process_and_releases_slot(
    fake_observe_bin, tmp_path, monkeypatch
):
    from herdeck import bridge as bridge_mod

    pidfile = tmp_path / "observe.pid"
    monkeypatch.setenv("FAKE_OBSERVE_PIDFILE", str(pidfile))
    ws, server, btask = await _open_bridge_client()
    try:
        await ws.send(
            json.dumps(
                {
                    "type": "observe",
                    "req": "disconnect",
                    "pane_id": "w1:p1",
                    "cols": 80,
                    "rows": 24,
                }
            )
        )
        await _recv_term(ws, "term_frame", "disconnect")
        pid = int(pidfile.read_text())
        await ws.close()
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("observe subprocess survived websocket disconnect")
        assert bridge_mod._observe_total == 0
    finally:
        await ws.close()
        btask.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await btask
        server.close()
        await server.wait_closed()


async def test_connection_send_lock_serializes_broadcast_reply_and_frames():
    from herdeck.bridge import _send_to_client

    active = 0
    max_active = 0

    class DetectConcurrentSend:
        async def send(self, msg):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1

    ws = DetectConcurrentSend()
    lock = asyncio.Lock()
    delivered = await asyncio.gather(
        _send_to_client(ws, "snapshot", lock, timeout=1),
        _send_to_client(ws, "reply", lock, timeout=1),
        _send_to_client(ws, "term_frame", lock, timeout=1),
    )
    assert delivered == [True, True, True]
    assert max_active == 1


async def test_send_timeout_starts_after_connection_lock_is_acquired():
    from herdeck.bridge import _send_to_client

    slow_started = asyncio.Event()

    class SlowFirstSend:
        async def send(self, msg):
            if msg == "slow":
                slow_started.set()
                await asyncio.sleep(0.05)

    ws = SlowFirstSend()
    lock = asyncio.Lock()
    slow = asyncio.create_task(_send_to_client(ws, "slow", lock, timeout=0.2))
    await slow_started.wait()
    # This message may wait >10 ms for the previous whole frame. Its own send
    # is instant, so it must not inherit time spent waiting for the lock.
    fast = await _send_to_client(ws, "fast", lock, timeout=0.01)
    assert await slow is True
    assert fast is True
