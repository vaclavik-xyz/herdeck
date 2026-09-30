"""Bridge-owned Telegram config document and bot-token store.

The document is a separate revisioned TOML file (not part of the shared
settings) so older runtimes that reject unknown shared sections are unaffected.
The bot token lives in its own 0600 file (or the ``HERDECK_TELEGRAM_TOKEN``
env var, which wins). The token never appears in frames, results, logs or
exception messages. Used from the bridge event loop only (no locking).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

from herdeck._atomic import atomic_write_private
from herdeck.bridge_settings import PutResult
from herdeck.config import ConfigError
from herdeck.i18n import LANGUAGES
from herdeck.shared_settings import MAX_SHARED_BYTES

log = logging.getLogger(__name__)

TELEGRAM_CONFIG_CAPABILITY = "telegram_config"
TELEGRAM_CAPABILITY = "telegram"

ENV_DOC = "HERDECK_BRIDGE_TELEGRAM"
ENV_TOKEN = "HERDECK_TELEGRAM_TOKEN"
ENV_TOKEN_FILE = "HERDECK_TELEGRAM_TOKEN_FILE"

_META = ("revision", "updated_at_ms", "updated_by")
# Used with fullmatch(): a trailing newline must not slip past `$`.
_TOKEN_RE = re.compile(r"[0-9]{5,16}:[A-Za-z0-9_-]{30,64}")
_CHAT_RE = re.compile(r"-?[0-9]{1,20}|@[A-Za-z0-9_]{5,32}")


@dataclass
class TelegramSettings:
    enabled: bool = False
    chat_id: str = ""
    message_thread_id: int | None = None
    interactive: bool = False
    allowed_user_ids: list[int] = field(default_factory=list)
    prompt_max_chars: int = 1200
    only_when_away: int = 0
    language: str = "en"
    sound: bool = True


def _is_int(v: object) -> bool:
    return type(v) is int


def parse_telegram(raw: object) -> TelegramSettings:
    """Strictly parse a raw document body. Error text never echoes values."""
    if not isinstance(raw, dict):
        raise ConfigError("telegram settings must be a table")
    known = set(TelegramSettings.__dataclass_fields__)
    unknown = sorted(str(k) for k in raw if k not in known)
    if unknown:
        raise ConfigError(f"unknown telegram key(s): {', '.join(unknown)}")
    s = TelegramSettings()
    for key in ("enabled", "interactive", "sound"):
        if key in raw:
            if type(raw[key]) is not bool:
                raise ConfigError(f"{key} must be a boolean")
            setattr(s, key, raw[key])
    if "chat_id" in raw:
        cid = raw["chat_id"]
        if not isinstance(cid, str) or (cid and not _CHAT_RE.fullmatch(cid)):
            raise ConfigError("chat_id must be a numeric id or @channel name")
        s.chat_id = cid
    if "message_thread_id" in raw:
        t = raw["message_thread_id"]
        if t is not None and (not _is_int(t) or t < 1):
            raise ConfigError("message_thread_id must be a positive integer")
        s.message_thread_id = t
    if "allowed_user_ids" in raw:
        ids = raw["allowed_user_ids"]
        if not isinstance(ids, list) or not all(_is_int(i) and i > 0 for i in ids):
            raise ConfigError("allowed_user_ids must be a list of positive integers")
        s.allowed_user_ids = list(ids)
    for key, lo, hi in (("prompt_max_chars", 200, 4000), ("only_when_away", 0, 1440)):
        if key in raw:
            v = raw[key]
            if not _is_int(v) or not lo <= v <= hi:
                raise ConfigError(f"{key} must be an integer in {lo}..{hi}")
            setattr(s, key, v)
    if "language" in raw:
        if raw["language"] not in LANGUAGES:
            raise ConfigError(f"language must be one of {', '.join(LANGUAGES)}")
        s.language = raw["language"]
    if s.interactive and not s.allowed_user_ids:
        raise ConfigError("interactive requires a non-empty allowed_user_ids")
    return s


def to_raw_telegram(s: TelegramSettings) -> dict:
    """JSON/TOML-safe dict (message_thread_id omitted when unset: TOML has no null)."""
    out: dict = {
        "enabled": s.enabled,
        "chat_id": s.chat_id,
        "interactive": s.interactive,
        "allowed_user_ids": list(s.allowed_user_ids),
        "prompt_max_chars": s.prompt_max_chars,
        "only_when_away": s.only_when_away,
        "language": s.language,
        "sound": s.sound,
    }
    if s.message_thread_id is not None:
        out["message_thread_id"] = s.message_thread_id
    return out


def default_paths(session: str | None = None) -> tuple[Path, Path]:
    """(document path, token path). ``session`` selects the embedded local bridge."""
    base = Path.home() / ".config/herdeck"
    if session is None:
        doc = os.environ.get(ENV_DOC)
        tok = os.environ.get(ENV_TOKEN_FILE)
        return (
            Path(doc) if doc else base / "bridge-telegram.toml",
            Path(tok) if tok else base / "telegram-token",
        )
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session)
    return base / f"local-bridge-telegram-{safe}.toml", base / f"local-telegram-token-{safe}"


class BridgeTelegramStore:
    def __init__(
        self,
        path: Path,
        token_path: Path,
        *,
        clock=time.time,
        env: Mapping[str, str] = os.environ,
    ) -> None:
        self.path = Path(path)
        self.token_path = Path(token_path)
        self._clock = clock
        self._env = env
        self.revision = 0
        self.updated_at_ms = 0
        self.updated_by = ""
        self.raw: dict | None = None
        self.error: str | None = None
        # Sources ("env"/"file") whose invalid token was already warned about;
        # a source that turns valid (or empty) is dropped so a new typo warns again.
        self._invalid_warned: set[str] = set()
        self._load()

    @property
    def settings(self) -> TelegramSettings:
        return parse_telegram(self.raw) if self.raw is not None else TelegramSettings()

    def _load(self) -> None:
        try:
            data = tomllib.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            self._load_failed(exc)
            return
        try:
            revision = data.get("revision", 0)
            at = data.get("updated_at_ms", 0)
            by = data.get("updated_by", "")
            if (
                type(revision) is not int or revision < 1
                or type(at) is not int or at < 0
                or not isinstance(by, str)
            ):
                raise ConfigError("invalid revision metadata")
            raw = to_raw_telegram(parse_telegram({k: v for k, v in data.items() if k not in _META}))
        except ConfigError as exc:
            self._load_failed(exc)
            return
        self.revision, self.updated_at_ms, self.updated_by, self.raw = revision, at, by, raw

    def _load_failed(self, exc: Exception) -> None:
        self.error = f"{self.path}: {exc}"
        log.error("bridge telegram config unreadable, serving as unset: %s", self.error)

    def put(self, base_revision: int, raw: object, by: str) -> PutResult:
        if type(base_revision) is not int or base_revision != self.revision:
            return PutResult(False, self.revision, "stale_revision")
        try:
            size = len(json.dumps(raw, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError):
            return PutResult(False, self.revision, "invalid", ["settings is not JSON-serializable"])
        if size > MAX_SHARED_BYTES:
            return PutResult(False, self.revision, "too_large",
                             [f"settings exceed {MAX_SHARED_BYTES} bytes"])
        try:
            normalized = to_raw_telegram(parse_telegram(raw))
        except ConfigError as exc:
            return PutResult(False, self.revision, "invalid", [str(exc)])
        revision = self.revision + 1
        at = int(self._clock() * 1000)
        by = str(by)
        try:
            doc = {"revision": revision, "updated_at_ms": at, "updated_by": by, **normalized}
            atomic_write_private(self.path, tomli_w.dumps(doc).encode("utf-8"))
        except UnicodeError:
            return PutResult(False, self.revision, "invalid", ["settings are not valid UTF-8"])
        self.revision, self.updated_at_ms, self.updated_by = revision, at, by
        self.raw, self.error = normalized, None
        return PutResult(True, revision)

    # -- token -------------------------------------------------------------

    def _checked(self, source: str, v: str) -> str | None:
        """``v`` when it is a well-formed token. A non-empty malformed value
        is warned about once per source (never the value, nor any part of it),
        so a typo does not silently read as "not set"."""
        if _TOKEN_RE.fullmatch(v):
            self._invalid_warned.discard(source)
            return v
        if not v:
            self._invalid_warned.discard(source)
        elif source not in self._invalid_warned:
            self._invalid_warned.add(source)
            where = ENV_TOKEN if source == "env" else f"token file {self.token_path}"
            log.warning(
                "telegram bot token from %s is malformed (expected <digits>:<30-64 chars>);"
                " ignoring it", where,
            )
        return None

    def _env_token(self) -> str | None:
        v = self._env.get(ENV_TOKEN)
        return self._checked("env", v.strip() if v else "")

    def token(self) -> str | None:
        env = self._env_token()
        if env:
            return env
        try:
            v = self.token_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            v = ""
        except (OSError, UnicodeDecodeError):
            return None
        return self._checked("file", v)

    def token_source(self) -> str | None:
        if self._env_token():
            return "env"
        return "file" if self.token() else None

    def set_token(self, token: object) -> str | None:
        if self._env_token():
            return "env_locked"
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            return "invalid"
        atomic_write_private(self.token_path, token.encode("ascii"))
        return None

    def clear_token(self) -> str | None:
        if self._env_token():
            return "env_locked"
        try:
            self.token_path.unlink()
        except FileNotFoundError:
            pass
        return None

    def frame(self, server_id: str, status: dict) -> dict:
        """Wire frame; ``settings`` is null while the document is unset."""
        return {
            "type": "telegram",
            "server_id": server_id,
            "revision": self.revision,
            "updated_at_ms": self.updated_at_ms,
            "updated_by": self.updated_by,
            "settings": self.raw,
            "status": status,
        }
