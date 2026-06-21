import pytest

from herdeck.bootstrap import resolve_mode, resolve_runtime_config
from herdeck.config import Config, ServerConfig


def _cfg(servers):
    return Config(servers=servers, profiles={}, overview_order=[], grid=(5, 3))


def test_resolve_mode_still_importable_and_remote():
    assert resolve_mode(mock=False, config_path="/c.toml", config_has_servers=True,
                        socket_path="/x.sock", socket_exists=False) == ("remote", "/c.toml")


@pytest.mark.asyncio
async def test_resolve_runtime_config_remote_passthrough():
    cfg = _cfg([ServerConfig("dev", "ws://x", "tok")])
    out, aclose = await resolve_runtime_config(("remote", "/c.toml"), cfg)
    assert out is cfg
    await aclose()  # no-op, must not raise


@pytest.mark.asyncio
async def test_resolve_runtime_config_local_starts_bridge(monkeypatch):
    closed = {"server": False, "task": False}

    class FakeServer:
        def close(self):
            closed["server"] = True

        async def wait_closed(self):
            pass

    class FakeTask:
        def cancel(self):
            closed["task"] = True

    async def fake_start_local_bridge(socket_path):
        return ("127.0.0.1", 5555, "tok", (FakeServer(), FakeTask()))

    monkeypatch.setattr("herdeck.bootstrap.start_local_bridge", fake_start_local_bridge)
    out, aclose = await resolve_runtime_config(("local", "/h.sock"), None)
    assert out.servers[0].url == "ws://127.0.0.1:5555"
    await aclose()
    assert closed == {"server": True, "task": True}
