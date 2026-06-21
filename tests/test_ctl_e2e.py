"""End-to-end: CtlSession over a real Connector + real loopback bridge + StubHerdr."""

import pytest

from herdeck.bridge import StubHerdr, start_local_bridge
from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig
from herdeck.ctl import CtlSession
from herdeck.model import AgentKey


def _pane(status="blocked"):
    return {
        "pane_id": "w1:p1",
        "agent": "claude",
        "agent_status": status,
        "foreground_cwd": "/proj/herdeck",
        "workspace_id": "w1",
    }


def _config(host, port, token):
    return Config(servers=[ServerConfig("local", f"ws://{host}:{port}", token)],
                  profiles=dict(DEFAULT_PROFILES), overview_order=["local"], grid=(5, 3))


@pytest.mark.asyncio
async def test_e2e_open_and_approve_blocked():
    herdr = StubHerdr([_pane("blocked")])
    host, port, token, (server, btask) = await start_local_bridge("/nonexistent.sock", herdr=herdr)
    try:
        sess = CtlSession(_config(host, port, token))
        await sess.open(timeout=5)
        assert AgentKey("local", "w1:p1") in sess.agents
        agent = sess.resolve_target("local:w1:p1")
        # settle_timeout=None: this checks the bridge round-trip, not settle (unit-tested)
        out = await sess.act("approve", agent, force=False, always=False,
                             settle_timeout=None, request_timeout=5)
        assert out["result"] == "sent"
        assert herdr.sent == [("w1:p1", ["1", "enter"])]  # claude approve profile keys
        await sess.close()
    finally:
        btask.cancel()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_e2e_approve_idle_is_skipped():
    herdr = StubHerdr([_pane("idle")])  # not blocked -> the bridge's act guard skips
    host, port, token, (server, btask) = await start_local_bridge("/nonexistent.sock", herdr=herdr)
    try:
        sess = CtlSession(_config(host, port, token))
        await sess.open(timeout=5)
        agent = sess.resolve_target("local:w1:p1")
        out = await sess.act("approve", agent, force=False, always=False,
                             settle_timeout=1, request_timeout=5)
        assert out["result"] == "skipped"
        assert herdr.sent == []  # no keys reached herdr
        await sess.close()
    finally:
        btask.cancel()
        server.close()
        await server.wait_closed()
