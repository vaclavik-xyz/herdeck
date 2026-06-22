import pytest

from herdeck.bootstrap import resolve_mode, resolve_runtime_config
from herdeck.config import Config, ServerConfig


def _cfg(servers):
    return Config(servers=servers, profiles={}, overview_order=[], grid=(5, 3))


def test_resolve_mode_still_importable_and_remote():
    assert resolve_mode(
        mock=False, config_path="/c.toml", config_has_servers=True, socket_path="/x.sock", socket_exists=False
    ) == ("remote", "/c.toml")


def test_discover_local_config_next_to_config(monkeypatch, tmp_path):
    from herdeck.bootstrap import _discover_local_config_path

    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    local = tmp_path / "local.toml"

    monkeypatch.delenv("HERDECK_LOCAL_CONFIG", raising=False)

    assert _discover_local_config_path(str(cfg)) == str(local)


def test_discover_local_config_prefers_env(monkeypatch, tmp_path):
    from herdeck.bootstrap import _discover_local_config_path

    env = tmp_path / "device.toml"
    monkeypatch.setenv("HERDECK_LOCAL_CONFIG", str(env))

    assert _discover_local_config_path("/x/config.toml") == str(env)


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


def test_make_runtime_profile_switcher_preserves_bridge_server():
    from herdeck.bootstrap import local_config, make_runtime_profile_switcher
    from herdeck.config import Config

    resolved = Config(servers=[], profiles={}, overview_order=[], grid=(4, 4))
    resolved.meta.active_profile = "mobile"
    resolved.meta.profile_names = ["work", "mobile"]
    resolved.theme.colors["blocked"] = "pink"
    resolved.view.show_profile_on_panel = True
    resolved.safety.approve_always = False
    resolved.hardware.tick_interval = 1.25
    runtime = local_config(7654, "secret", resolved)
    switch = make_runtime_profile_switcher(runtime, lambda name: resolved, local_bridge=True)

    cfg = switch("mobile")

    assert cfg.servers[0].id == "local"
    assert cfg.servers[0].url == "ws://127.0.0.1:7654"
    assert cfg.servers[0].token == "secret"
    assert cfg.grid == (4, 4)
    assert cfg.meta.active_profile == "mobile"
    assert cfg.meta.profile_names == ["work", "mobile"]
    assert cfg.theme.colors["blocked"] == "pink"
    assert cfg.view.show_profile_on_panel is True
    assert cfg.safety.approve_always is False
    assert cfg.hardware.tick_interval == 1.25


def test_make_runtime_profile_switcher_keeps_remote_servers_named_local():
    from herdeck.bootstrap import make_runtime_profile_switcher
    from herdeck.config import Config, ServerConfig

    runtime = Config(
        servers=[ServerConfig("local", "wss://old", "old-token")],
        profiles={},
        overview_order=["local"],
        grid=(5, 3),
    )
    resolved = Config(
        servers=[ServerConfig("prod", "wss://new", "new-token")],
        profiles={},
        overview_order=["prod"],
        grid=(5, 3),
    )
    switch = make_runtime_profile_switcher(runtime, lambda name: resolved)

    cfg = switch("work")

    assert cfg.servers == resolved.servers
    assert cfg.overview_order == ["prod"]
