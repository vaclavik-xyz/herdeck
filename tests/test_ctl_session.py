import asyncio

import pytest

from herdeck.commands import Command
from herdeck.config import Config, ServerConfig
from herdeck.ctl import ConnectionLost, CtlSession
from herdeck.model import AgentKey, AgentState, Status


class FakeConnector:
    """Drop-in for Connector: records sent msgs, exposes its callbacks to the test."""
    def __init__(self, server, on_snapshot, on_event, on_connection, on_result, on_error):
        self.server = server
        self.on_snapshot, self.on_event = on_snapshot, on_event
        self.on_connection, self.on_result, self.on_error = on_connection, on_result, on_error
        self.sent: list[dict] = []
        self._run = asyncio.Event()

    async def run(self):
        self.on_connection(self.server.id, True)
        await self._run.wait()  # block until stop()

    def stop(self):
        self._run.set()

    async def send(self, msg):
        self.sent.append(msg)


def _config():
    return Config(servers=[ServerConfig("dev", "ws://x", "tok")],
                  profiles={}, overview_order=[], grid=(5, 3))


def _agent(status=Status.BLOCKED):
    return AgentState(AgentKey("dev", "p1"), "claude", "lbl", status)


@pytest.mark.asyncio
async def test_open_waits_for_snapshot_then_request_correlates():
    fc = {}

    def factory(**kw):
        fc["c"] = FakeConnector(**kw)
        return fc["c"]

    sess = CtlSession(_config(), connector_factory=factory)
    open_task = asyncio.create_task(sess.open(timeout=1))
    await asyncio.sleep(0)  # let run() fire on_connection + register
    fc["c"].on_snapshot("dev", [_agent()])
    await open_task
    assert AgentKey("dev", "p1") in sess.agents

    req_task = asyncio.create_task(sess.request(Command("focus", "dev", "p1"), timeout=1))
    await asyncio.sleep(0)
    sent = fc["c"].sent[-1]
    fc["c"].on_result(sent["req"], {"focused": True})
    assert await req_task == {"focused": True}
    await sess.close()


@pytest.mark.asyncio
async def test_open_snapshot_timeout_raises():
    sess = CtlSession(_config(), connector_factory=lambda **kw: FakeConnector(**kw))
    with pytest.raises(ConnectionLost):
        await sess.open(timeout=0.05)
    await sess.close()


@pytest.mark.asyncio
async def test_request_fails_on_connection_drop():
    fc = {}
    sess = CtlSession(_config(),
                      connector_factory=lambda **kw: fc.setdefault("c", FakeConnector(**kw)))
    open_task = asyncio.create_task(sess.open(timeout=1))
    await asyncio.sleep(0)
    fc["c"].on_snapshot("dev", [_agent()])
    await open_task
    req_task = asyncio.create_task(sess.request(Command("focus", "dev", "p1"), timeout=5))
    await asyncio.sleep(0)
    fc["c"].on_connection("dev", False)  # drop before result
    with pytest.raises(ConnectionLost):
        await req_task
    await sess.close()
