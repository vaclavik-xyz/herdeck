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


@pytest.mark.asyncio
async def test_wait_returns_immediately_when_already_satisfied():
    sess = CtlSession(_config())
    sess.agents[AgentKey("dev", "p1")] = _agent(Status.BLOCKED)

    def pred():
        a = sess.agents.get(AgentKey("dev", "p1"))
        return a if a and a.status is Status.BLOCKED else None

    assert await sess.wait(pred, timeout=1) is not None


@pytest.mark.asyncio
async def test_wait_wakes_on_event_and_ignores_foreign_changes():
    sess = CtlSession(_config())
    target = AgentKey("dev", "p1")

    def pred():
        a = sess.agents.get(target)
        return a if a and a.status is Status.BLOCKED else None

    wait_task = asyncio.create_task(sess.wait(pred, timeout=1))
    await asyncio.sleep(0)
    # foreign agent change must NOT satisfy the wait
    sess._on_event("dev", AgentState(AgentKey("dev", "p2"), "claude", "x", Status.BLOCKED))
    await asyncio.sleep(0)
    assert not wait_task.done()
    # target blocks -> wait returns it
    sess._on_event("dev", _agent(Status.BLOCKED))
    assert (await wait_task).key == target


@pytest.mark.asyncio
async def test_wait_times_out_cleanly_to_none():
    sess = CtlSession(_config())
    assert await sess.wait(lambda: None, timeout=0.05) is None


@pytest.mark.asyncio
async def test_settle_true_when_agent_leaves_blocked():
    sess = CtlSession(_config())
    sess.agents[AgentKey("dev", "p1")] = _agent(Status.BLOCKED)
    settle_task = asyncio.create_task(sess.settle(_agent(Status.BLOCKED), timeout=1))
    await asyncio.sleep(0)
    sess._on_event("dev", _agent(Status.WORKING))
    assert await settle_task is True


@pytest.mark.asyncio
async def test_settle_false_on_timeout_when_still_blocked():
    sess = CtlSession(_config())
    sess.agents[AgentKey("dev", "p1")] = _agent(Status.BLOCKED)
    assert await sess.settle(_agent(Status.BLOCKED), timeout=0.05) is False


@pytest.mark.asyncio
async def test_request_list_raises():
    # N3: list has no request/response (bridge replies with a snapshot, not a result)
    sess = CtlSession(_config())
    with pytest.raises(ValueError):
        await sess.request(Command("list", "dev"), timeout=1)


@pytest.mark.asyncio
async def test_request_after_drop_raises_immediately():
    # N4: connection dropped BEFORE the send -> immediate ConnectionLost, not a timeout
    fc = {}
    sess = CtlSession(_config(),
                      connector_factory=lambda **kw: fc.setdefault("c", FakeConnector(**kw)))
    open_task = asyncio.create_task(sess.open(timeout=1))
    await asyncio.sleep(0)
    fc["c"].on_snapshot("dev", [_agent()])
    await open_task
    fc["c"].on_connection("dev", False)  # drop before we issue the request
    with pytest.raises(ConnectionLost):
        await sess.request(Command("focus", "dev", "p1"), timeout=5)
    assert fc["c"].sent == []  # nothing was silently swallowed by connector.send
    await sess.close()
