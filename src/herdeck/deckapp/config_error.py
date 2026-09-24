"""The deck state while an existing config cannot be loaded.

A config file that is present but broken (a missing bridge token, a malformed
value) used to fall back to the demo MockSource: the deck then showed fake
agents and looked healthy. This source shows an explicit error instead — an
empty grid, a red panel naming the problem — and ``/health`` / ``/maintenance``
carry the message. The deck recovers on its own once the config loads (the
config watcher re-selects the source).
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

from ..config import DEFAULT_PROFILES, Config, HardwareConfig, ViewConfig
from ..i18n import LANGUAGES
from ..orchestrator import Orchestrator
from .source import StateSource

log = logging.getLogger("herdeck.deckapp.config_error")

# The last message logged, so a watcher re-probe of the same broken config does
# not repeat the ERROR line every few seconds.
_last_logged: str | None = None


def log_config_error(message: str | None) -> None:
    """Log a config error at ERROR once per distinct message (None = recovered)."""
    global _last_logged
    if message and message != _last_logged:
        log.error("config cannot be loaded; the deck shows the error (not demo agents): %s", message)
    elif message is None and _last_logged is not None:
        log.warning("config loads again; leaving the config-error state")
    _last_logged = message


def _raw_language(config_path: str | None) -> str:
    """``[view].language`` read straight from the TOML (best effort): the config
    as a whole did not load, but the error should still speak the user's language."""
    if not config_path:
        return "en"
    try:
        data = tomllib.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return "en"
    view = data.get("view")
    lang = view.get("language") if isinstance(view, dict) else None
    return lang if lang in LANGUAGES else "en"


class ConfigErrorSource(StateSource):
    """No agents, never connected, a panel that names the config problem."""

    source_name = "config_error"

    def __init__(
        self,
        message: str,
        *,
        server_id: str | None = None,
        hardware: HardwareConfig | None = None,
        language: str = "en",
    ) -> None:
        self.message = message
        self.error_server_id = server_id
        self._config = Config(
            servers=[],
            profiles=dict(DEFAULT_PROFILES),
            overview_order=[],
            grid=(5, 3),
            view=ViewConfig(language=language),
            hardware=hardware or HardwareConfig(),
        )

    @property
    def config(self) -> Config:
        return self._config

    @property
    def connected(self) -> bool:
        return False

    def apply_to(self, orch: Orchestrator) -> None:
        orch.set_config_error(True, self.error_server_id)

    def press(self, index: int) -> None:
        return None  # nothing to act on until the config loads

    def summary(self) -> dict:
        return {"agents": 0, "blocked": 0, "working": 0, "idle": 0, "done": 0, "waiting": 0}


def config_error_source(error, config_path: str | None, hardware: HardwareConfig | None):
    """Build the source for a load error (a ConfigError, or an OSError reading it)."""
    from ..settings import TokenNotFoundError

    server_id = error.server_id if isinstance(error, TokenNotFoundError) else None
    message = str(error) or type(error).__name__
    log_config_error(message)
    return ConfigErrorSource(
        message,
        server_id=server_id,
        hardware=hardware,
        language=_raw_language(config_path),
    )
