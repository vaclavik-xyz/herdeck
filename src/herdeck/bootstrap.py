from __future__ import annotations

import os
from collections.abc import Awaitable, Callable

from .bridge import start_local_bridge
from .config import (
    DEFAULT_MACROS,
    DEFAULT_PROFILES,
    DEFAULT_START_PROFILES,
    Config,
    ConfigMeta,
    HardwareConfig,
    Notifications,
    SafetyConfig,
    ServerConfig,
    ThemeConfig,
    ViewConfig,
)


def resolve_socket_path(config=None, *, getenv=os.environ.get) -> str:
    """Resolve the herdr Unix socket path: HERDR_SOCKET env, else the config's
    hardware override, else the XDG default. Shared by the CLI and the deckapp."""
    raw = getenv("HERDR_SOCKET") or (
        config.hardware.herdr_socket if config and config.hardware.herdr_socket else None
    )
    return os.path.expanduser(raw or "~/.config/herdr/herdr.sock")


def resolve_mode(*, mock, config_path, config_has_servers, socket_path, socket_exists):
    """Decide how to run from already-gathered facts (pure; no IO)."""
    if mock:
        return ("mock",)
    if config_path is not None and config_has_servers:
        return ("remote", config_path)
    if socket_exists:
        return ("local", socket_path)
    return (
        "error",
        f"No herdr socket at {socket_path} and no [[servers]] config. "
        f"Is herdr running? Set HERDR_SOCKET or create a config "
        f"(see config.example.toml).",
    )


def _local_config_for_server(server: ServerConfig, partial=None) -> Config:
    profiles = dict(DEFAULT_PROFILES)
    if partial is not None:
        profiles.update(partial.profiles)
    return Config(
        servers=[server],
        profiles=profiles,
        overview_order=[server.id],
        grid=partial.grid if partial else (5, 3),
        macros=partial.macros if partial else list(DEFAULT_MACROS),
        start_profiles=(partial.start_profiles if partial else dict(DEFAULT_START_PROFILES)),
        notifications=partial.notifications if partial else Notifications(),
        theme=partial.theme if partial else ThemeConfig(),
        view=partial.view if partial else ViewConfig(),
        safety=partial.safety if partial else SafetyConfig(),
        hardware=partial.hardware if partial else HardwareConfig(),
        meta=partial.meta if partial else ConfigMeta(),
    )


def local_config(port, token, partial=None):
    """Synthesize the config for local mode from the bound bridge port/token."""
    return _local_config_for_server(
        ServerConfig("local", f"ws://127.0.0.1:{port}", token),
        partial,
    )


def make_runtime_profile_switcher(
    runtime_config: Config,
    switch_profile,
    *,
    local_bridge: bool = False,
):
    if switch_profile is None:
        return None
    local_server = (
        runtime_config.servers[0]
        if local_bridge and len(runtime_config.servers) == 1
        else None
    )

    def switch(name: str) -> Config | None:
        resolved = switch_profile(name)
        if resolved is None:
            return None
        if local_server is None:
            return resolved
        return _local_config_for_server(local_server, resolved)

    return switch


def _discover_config_path():
    p = os.environ.get("HERDECK_CONFIG")
    if p:
        return os.path.abspath(p)
    for cand in (
        os.path.expanduser("~/.config/herdeck/config.toml"),
        os.path.abspath("config.toml"),
    ):
        if os.path.exists(cand):
            return cand
    return None


def _discover_local_config_path(config_path: str | None):
    p = os.environ.get("HERDECK_LOCAL_CONFIG")
    if p:
        return os.path.abspath(p)
    if config_path:
        return os.path.join(os.path.dirname(config_path), "local.toml")
    return os.path.expanduser("~/.config/herdeck/local.toml")


async def resolve_runtime_config(
    mode: tuple, file_config: Config | None
) -> tuple[Config, Callable[[], Awaitable[None]]]:
    """Produce a connected Config + an async cleanup for the 'remote'/'local' modes.

    'mock' and 'error' are handled by callers, not here.
    """

    async def _noop() -> None:
        return None

    if mode[0] == "remote":
        return file_config, _noop
    if mode[0] == "local":
        _host, port, token, handle = await start_local_bridge(mode[1])
        server, btask = handle

        async def _close() -> None:
            btask.cancel()
            server.close()
            await server.wait_closed()

        return local_config(port, token, file_config), _close
    raise ValueError(f"cannot resolve runtime config for mode {mode!r}")
