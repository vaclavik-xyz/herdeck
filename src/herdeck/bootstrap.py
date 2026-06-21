from __future__ import annotations

import os
from collections.abc import Awaitable, Callable

from .bridge import start_local_bridge
from .config import (
    DEFAULT_MACROS,
    DEFAULT_PROFILES,
    DEFAULT_START_PROFILES,
    Config,
    Notifications,
    ServerConfig,
)


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


def local_config(port, token, partial=None):
    """Synthesize the config for local mode from the bound bridge port/token."""
    profiles = dict(DEFAULT_PROFILES)
    if partial is not None:
        profiles.update(partial.profiles)
    return Config(
        servers=[ServerConfig("local", f"ws://127.0.0.1:{port}", token)],
        profiles=profiles,
        overview_order=["local"],
        grid=partial.grid if partial else (5, 3),
        macros=partial.macros if partial else list(DEFAULT_MACROS),
        start_profiles=(partial.start_profiles if partial else dict(DEFAULT_START_PROFILES)),
        notifications=partial.notifications if partial else Notifications(),
    )


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
