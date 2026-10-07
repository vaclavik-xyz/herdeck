"""Deck theme registry: the built-ins plus user themes found in
``<config dir>/themes/<id>.py`` (docs/themes.md).

A user theme is CODE with the user's full privileges; it is loaded only when
its file sits in that folder (installing one is a deliberate act). A file that
fails to import, lacks ``THEME``, declares another ``api_version`` or another
``id`` is listed with ``error`` and renders as ``classic``. The folder is
rescanned at most every ``RESCAN_S`` seconds, so an edited theme reloads by
itself; each theme's ``fingerprint`` (file mtime + size) goes into the render
cache keys, so an edit re-renders the deck.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..config import DECK_THEME_ID_PATTERN, DEFAULT_DECK_THEME
from .api import API_VERSION, Theme

log = logging.getLogger(__name__)

# Built-in themes: id -> module exposing THEME. Registering a built-in is ONE
# line here (the module is imported lazily; the PyInstaller specs bundle every
# herdeck.themes submodule via collect_submodules).
BUILTIN_MODULES: dict[str, str] = {
    "classic": "herdeck.themes.classic",
}
RESCAN_S = 2.0
_ID_RE = re.compile(DECK_THEME_ID_PATTERN)


@dataclass(frozen=True)
class ThemeInfo:
    """One row of ``ThemeRegistry.list()`` (and of ``GET /themes``)."""

    id: str
    name: dict[str, str] = field(default_factory=dict)
    builtin: bool = False
    error: str | None = None

    def as_json(self) -> dict:
        return {"id": self.id, "name": dict(self.name), "builtin": self.builtin, "error": self.error}


@dataclass
class _UserTheme:
    path: str
    sig: tuple[int, int]  # (mtime_ns, size)
    theme: Theme | None
    error: str | None


def themes_dir() -> str:
    """``$HERDECK_THEMES_DIR``, else ``themes/`` next to the config file the
    runtime loads (``$HERDECK_CONFIG`` / ``~/.config/herdeck/config.toml``)."""
    env = os.environ.get("HERDECK_THEMES_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    from ..bootstrap import _discover_config_path

    config = _discover_config_path() or os.path.expanduser("~/.config/herdeck/config.toml")
    return os.path.join(os.path.dirname(config), "themes")


def _load_user_theme(theme_id: str, path: str) -> tuple[Theme | None, str | None]:
    """Import one theme file -> (theme, None) or (None, error text)."""
    module_name = f"herdeck_user_theme_{theme_id.replace('-', '_')}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return None, "not an importable Python file"
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module  # dataclasses & co. look themselves up here
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
        theme = getattr(module, "THEME", None)
        if not isinstance(theme, Theme):
            return None, "the file does not define THEME = <a herdeck.themes.api.Theme instance>"
        if theme.api_version != API_VERSION:
            return None, (
                f"THEME.api_version is {theme.api_version!r}; "
                f"this herdeck supports {API_VERSION}"
            )
        if theme.id != theme_id:
            return None, f"THEME.id {theme.id!r} does not match the file name {theme_id!r}"
        if theme.base is not None and not isinstance(theme.base, str):
            return None, "THEME.base must be a theme id or None"
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # SyntaxError, SystemExit, anything the file raises
        return None, f"{type(exc).__name__}: {exc}"
    return theme, None


class ThemeRegistry:
    """Built-in + user themes. Thread-safe; ``resolve``/``chain`` never raise."""

    def __init__(
        self,
        directory: str | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        rescan_s: float = RESCAN_S,
        builtins: dict[str, str] | None = None,
    ):
        self._dir = directory
        self._clock = clock
        self._rescan_s = rescan_s
        self._builtin_modules = dict(BUILTIN_MODULES if builtins is None else builtins)
        self._builtins: dict[str, Theme] = {}
        self._builtin_errors: dict[str, str] = {}
        self._user: dict[str, _UserTheme] = {}
        self._shadowed: dict[str, str] = {}  # user files named like a built-in
        self._scanned_at: float | None = None
        self._warned: set[tuple[str, str]] = set()
        self._lock = threading.RLock()

    # --- loading ---
    def _builtin(self, theme_id: str) -> Theme | None:
        if theme_id in self._builtins:
            return self._builtins[theme_id]
        if theme_id in self._builtin_errors:
            return None
        try:
            theme = importlib.import_module(self._builtin_modules[theme_id]).THEME
        except Exception as exc:  # a broken built-in must not take the deck down
            self._builtin_errors[theme_id] = f"{type(exc).__name__}: {exc}"
            log.exception("built-in deck theme %s failed to load", theme_id)
            return None
        self._builtins[theme_id] = theme
        return theme

    def _directory(self) -> str:
        if self._dir is None:
            self._dir = themes_dir()
        return self._dir

    def _maybe_scan(self, force: bool = False) -> None:
        now = self._clock()
        if not force and self._scanned_at is not None and now - self._scanned_at < self._rescan_s:
            return
        self._scanned_at = now
        directory = self._directory()
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            entries = []
        seen: set[str] = set()
        shadowed: dict[str, str] = {}
        for entry in entries:
            stem, ext = os.path.splitext(entry.name)
            if ext != ".py" or not _ID_RE.fullmatch(stem) or not entry.is_file():
                continue
            if stem in self._builtin_modules:
                shadowed[stem] = entry.path
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            sig = (st.st_mtime_ns, st.st_size)
            seen.add(stem)
            known = self._user.get(stem)
            if known is not None and known.sig == sig and known.path == entry.path:
                continue
            theme, error = _load_user_theme(stem, entry.path)
            if error:
                log.warning("deck theme %s (%s) not loaded: %s", stem, entry.path, error)
            self._user[stem] = _UserTheme(entry.path, sig, theme, error)
        for gone in set(self._user) - seen:
            del self._user[gone]
        for stem, path in shadowed.items():
            if stem not in self._shadowed:
                log.warning("deck theme file %s ignored: %r is a built-in theme", path, stem)
        self._shadowed = shadowed

    def _get(self, theme_id: str) -> Theme | None:
        if theme_id in self._builtin_modules:
            return self._builtin(theme_id)
        user = self._user.get(theme_id)
        return user.theme if user is not None else None

    def _warn_once(self, theme_id: str, what: str, msg: str, *args) -> None:
        if (theme_id, what) not in self._warned:
            self._warned.add((theme_id, what))
            log.warning(msg, *args)

    # --- public ---
    def list(self) -> list[ThemeInfo]:
        """Every theme the editor may offer: built-ins first, then user themes
        by id; broken ones carry ``error``."""
        with self._lock:
            self._maybe_scan(force=True)
            out = []
            for theme_id in self._builtin_modules:
                theme = self._builtin(theme_id)
                out.append(
                    ThemeInfo(
                        theme_id,
                        dict(getattr(theme, "name", {}) or {}),
                        True,
                        self._builtin_errors.get(theme_id),
                    )
                )
            for theme_id in sorted(self._user):
                user = self._user[theme_id]
                name = dict(getattr(user.theme, "name", {}) or {}) if user.theme else {}
                out.append(ThemeInfo(theme_id, name, False, user.error))
            for theme_id, path in sorted(self._shadowed.items()):
                out.append(
                    ThemeInfo(
                        theme_id,
                        {},
                        False,
                        f"{path} ignored: {theme_id!r} is a built-in theme",
                    )
                )
            return out

    def resolve(self, theme_id: str) -> Theme:
        """The theme drawn for ``theme_id`` — ``classic`` (with a one-time
        warning) for an unknown or broken id."""
        return self.chain(theme_id)[0]

    def chain(self, theme_id: str) -> tuple[Theme, ...]:
        """``theme_id``'s theme, then its ``base`` themes, always ending with
        ``classic``. A missing/broken base or a base cycle ends the chain early
        (at ``classic``)."""
        with self._lock:
            classic = self._builtin(DEFAULT_DECK_THEME)
            if theme_id == DEFAULT_DECK_THEME:
                return (classic,) if classic is not None else ()
            self._maybe_scan()
            out: list[Theme] = []
            seen: set[str] = set()
            current: str | None = theme_id
            while current and current != DEFAULT_DECK_THEME:
                if current in seen:
                    self._warn_once(theme_id, "cycle", "deck theme %r has a base cycle", theme_id)
                    break
                seen.add(current)
                theme = self._get(current)
                if theme is None:
                    self._warn_once(
                        current,
                        "missing",
                        "deck theme %r is unknown or failed to load; drawing %s instead",
                        current,
                        "its base" if out else "classic",
                    )
                    break
                out.append(theme)
                current = theme.base
            if classic is not None:
                out.append(classic)
            return tuple(out)

    def fingerprint(self, theme_id: str) -> str:
        """Changes whenever a theme in ``theme_id``'s chain is edited (user
        file mtime/size); "" for classic."""
        with self._lock:
            parts = []
            for theme in self.chain(theme_id):
                user = self._user.get(theme.id)
                parts.append(f"{theme.id}:{user.sig[0]}-{user.sig[1]}" if user else theme.id)
            return "|".join(parts) if parts != [DEFAULT_DECK_THEME] else ""


_DEFAULT: ThemeRegistry | None = None
_DEFAULT_LOCK = threading.Lock()


def default_registry() -> ThemeRegistry:
    """The process-wide registry every render path uses."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = ThemeRegistry()
        return _DEFAULT


def set_default_registry(registry: ThemeRegistry | None) -> None:
    """Swap the process-wide registry (tests); None = rebuild lazily."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = registry
