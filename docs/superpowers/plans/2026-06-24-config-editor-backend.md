# Config Editor — Backend Implementation Plan (Phase 2, part 1 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Python backend for the herdeck config editor — a `ConfigService` (read/validate/write the unified TOML config + manage profiles + keychain secrets), its loopback HTTP routes on the existing sidecar, and the A+B config reload (in-app sidecar reload + standalone-deck file-watch hot-reload).

**Architecture:** A new pure `ConfigService` reads/validates/writes the source TOML through the existing `settings`/`config` core (no new config logic). A new core `secrets` module backs token values with the OS keychain (`keyring`), env-first, and `settings._server_config` reads through it. The sidecar's `DeckApp` HTTP server gains config routes that delegate to `ConfigService` and an in-app `reload()`. A generic `ConfigWatcher` (mtime poll) drives both the sidecar's reload and `app.py`'s standalone hot-reload.

**Tech Stack:** Python ≥3.12, stdlib `tomllib` (read) + `tomli-w` (write), `keyring` (OS keychain), `http.server` (existing sidecar), pytest.

**Frontend follow-up:** The GUI (Tauri 2nd window, `configClient.ts`, `ConfigApp.svelte` + section forms) is a SEPARATE plan written against this implemented API. This plan ends with a fully pytest-tested backend (config read/validate/write/secrets/reload) reachable over the token-authed loopback HTTP.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-24-config-editor-design.md`.
- TDD: failing test first, watch it fail, minimal code, watch it pass, commit.
- Branch: `feat/config-editor` (already created off `main`; the spec commit `feca223` is on it).
- Run the full suite from repo root with the venv active: `source .venv/bin/activate`; `PYTHONPATH=. python -m pytest -q` (some tests import `tests.*`).
- `token_env` secrets stay out of TOML and out of any HTTP response body / log — TOML and `/config` carry only the `token_env` NAME and a `{set, source}` presence flag.
- Secret resolution order is **env-first, then keychain**: `os.environ.get(name)` wins; keychain is the fallback. Keep the existing `ConfigError("env var '{env}' for server '{id}' is not set")` message when neither is present.
- `keyring` service name is the literal `"herdeck"`.
- `keyring`/`tomli-w` imports degrade gracefully: a missing `keyring` backend on read returns "secret not set" (never raises); writing TOML always uses `tomli-w`.
- Atomic writes only: serialize to a temp file in the target dir, then `os.replace` onto the destination. Generated TOML starts with the header line `# Managed by herdeck-config — generated; manual comments are not preserved`.
- `"default"` is the reserved base profile name; `[profiles.default]` is invalid (already enforced by `validate_settings`).
- Reuse existing core: `settings.{load_settings,resolve_profile,validate_settings,set_active_profile,SettingsSnapshot}`, `config.ConfigError`, `bootstrap._discover_config_path/_discover_local_config_path`, `app.App._update_connectors`, `deckapp.server.DeckApp`. Do not reimplement them.

---

## File Structure

- `src/herdeck/secrets.py` — **new.** Keyring-backed secret store: `get_secret`, `set_secret`, `clear_secret`, `has_secret`, `secret_source`. Env-first, keyring fallback, graceful when keyring is absent.
- `src/herdeck/settings.py` — **modify** `_server_config` to resolve the token via `secrets.get_secret`.
- `src/herdeck/deckapp/config_service.py` — **new.** `ConfigService`: `read`, `validate`, `write`, `set_active`, `create_profile`, `delete_profile`.
- `src/herdeck/deckapp/watcher.py` — **new.** `ConfigWatcher`: a daemon thread that polls config/local mtimes and fires a callback on change (debounced).
- `src/herdeck/deckapp/server.py` — **modify** `DeckApp` to hold a `ConfigService`, add config HTTP routes, add `reload()`, and accept a config-reload trigger.
- `src/herdeck/app.py` — **modify**: extract `App._apply_config`, add `App.reload_from_disk`, add `make_config_reloader`, and wire a `ConfigWatcher` in `main()`.
- `pyproject.toml` — **modify**: add `keyring` (Task 1) and `tomli-w` (Task 4) to the `deck` and `dev` dependency groups.
- Tests: `tests/test_secrets.py`, `tests/test_config_service.py`, `tests/test_config_watcher.py`, additions to `tests/test_deckapp.py` and `tests/test_app.py`.

---

### Task 1: `secrets` module (keyring, env-first) + `_server_config` resolves through it

**Files:**
- Create: `src/herdeck/secrets.py`
- Modify: `src/herdeck/settings.py` (`_server_config`, ~lines 238-243)
- Modify: `pyproject.toml` (add `keyring` to `deck` and `dev` groups)
- Test: `tests/test_secrets.py`

**Interfaces:**
- Produces: `secrets.SERVICE = "herdeck"`; `secrets.get_secret(name: str) -> str | None` (env-first, keyring fallback, None if neither); `secrets.set_secret(name: str, value: str) -> None`; `secrets.clear_secret(name: str) -> None`; `secrets.has_secret(name: str) -> bool`; `secrets.secret_source(name: str) -> str | None` (`"env"` | `"keychain"` | `None`).
- `settings._server_config` now calls `secrets.get_secret(env)` instead of `os.environ.get(env)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_secrets.py`:

```python
import herdeck.secrets as secrets


class FakeKeyring:
    """In-memory stand-in for the `keyring` module surface used by secrets.py."""

    def __init__(self):
        self.store = {}

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        if (service, name) in self.store:
            del self.store[(service, name)]
        else:
            raise KeyError(name)


def test_get_secret_prefers_env_over_keychain(monkeypatch):
    fake = FakeKeyring()
    fake.set_password("herdeck", "TOK", "from_keychain")
    monkeypatch.setattr(secrets, "_keyring", lambda: fake)
    monkeypatch.setenv("TOK", "from_env")
    assert secrets.get_secret("TOK") == "from_env"
    assert secrets.secret_source("TOK") == "env"


def test_get_secret_falls_back_to_keychain(monkeypatch):
    fake = FakeKeyring()
    fake.set_password("herdeck", "TOK", "from_keychain")
    monkeypatch.setattr(secrets, "_keyring", lambda: fake)
    monkeypatch.delenv("TOK", raising=False)
    assert secrets.get_secret("TOK") == "from_keychain"
    assert secrets.secret_source("TOK") == "keychain"
    assert secrets.has_secret("TOK") is True


def test_set_and_clear_secret(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(secrets, "_keyring", lambda: fake)
    monkeypatch.delenv("TOK", raising=False)
    secrets.set_secret("TOK", "v")
    assert fake.store[("herdeck", "TOK")] == "v"
    secrets.clear_secret("TOK")
    assert secrets.has_secret("TOK") is False
    assert secrets.secret_source("TOK") is None


def test_missing_keyring_backend_degrades_to_env_only(monkeypatch):
    def boom():
        raise RuntimeError("no backend")

    monkeypatch.setattr(secrets, "_keyring", boom)
    monkeypatch.delenv("TOK", raising=False)
    assert secrets.get_secret("TOK") is None  # never raises
    assert secrets.has_secret("TOK") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && python -m pytest tests/test_secrets.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'herdeck.secrets'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/herdeck/secrets.py`:

```python
"""OS-keychain-backed secret store for herdeck token values.

Resolution is env-first: an environment variable named `token_env` always wins;
the OS keychain (via `keyring`, service "herdeck") is the fallback store the
config editor writes to. TOML never holds secret values — only the env-var name.
"""
from __future__ import annotations

import os

SERVICE = "herdeck"


def _keyring():
    """Return the keyring module, or raise if it is unavailable.

    Indirected through a function so tests can swap it and so an absent backend
    degrades gracefully (callers that read catch the failure).
    """
    import keyring

    return keyring


def get_secret(name: str) -> str | None:
    """The secret value for `name`: the env var if set, else the keychain entry,
    else None. Never raises if the keychain backend is missing."""
    env = os.environ.get(name)
    if env:
        return env
    try:
        return _keyring().get_password(SERVICE, name)
    except Exception:
        return None


def secret_source(name: str) -> str | None:
    """Where the secret comes from: "env", "keychain", or None if unset."""
    if os.environ.get(name):
        return "env"
    try:
        if _keyring().get_password(SERVICE, name) is not None:
            return "keychain"
    except Exception:
        return None
    return None


def has_secret(name: str) -> bool:
    return get_secret(name) is not None


def set_secret(name: str, value: str) -> None:
    """Store `value` in the OS keychain under `name`. Raises if no backend."""
    _keyring().set_password(SERVICE, name, value)


def clear_secret(name: str) -> None:
    """Remove the keychain entry for `name`. No-op if it is absent."""
    try:
        _keyring().delete_password(SERVICE, name)
    except Exception:
        pass
```

Modify `src/herdeck/settings.py` `_server_config` (replace the `os.environ.get(env)` lookup):

```python
def _server_config(raw: dict) -> ServerConfig:
    from .secrets import get_secret

    env = raw["token_env"]
    token = get_secret(env)
    if not token:
        raise ConfigError(f"env var '{env}' for server '{raw['id']}' is not set")
    return ServerConfig(raw["id"], raw["url"], token)
```

Modify `pyproject.toml`: add `"keyring"` to both the `deck` and `dev` optional-dependency lists, e.g.:

```toml
deck = ["strmdck", "pillow>=10", "cairosvg", "deepdiff", "python-dotenv", "hidapi", "keyring"]
dev = ["pytest>=8", "pytest-asyncio>=0.23", "pillow>=10", "cairosvg", "ruff", "pytest-cov", "keyring"]
```

Then install so `keyring` is importable: `python -m pip install -e '.[dev]' -q`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_secrets.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Run the full suite (no regressions from the `_server_config` change)**

Run: `PYTHONPATH=. python -m pytest -q`
Expected: all pass (the existing settings/config tests set `TOK`/`HERDECK_TOKEN` via env, which still wins env-first).

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/secrets.py src/herdeck/settings.py pyproject.toml tests/test_secrets.py
git commit -m "feat(secrets): keychain-backed token store, env-first; _server_config reads it"
```

---

### Task 2: `ConfigService.read()` — current config as a redacted editor dict

**Files:**
- Create: `src/herdeck/deckapp/config_service.py`
- Test: `tests/test_config_service.py`

**Interfaces:**
- Consumes: `settings.load_settings`, `config.ConfigError`, `secrets.has_secret`/`secret_source`.
- Produces: `ConfigService(config_path: str | Path, local_path: str | Path)`; `ConfigService.BASE_SECTIONS: tuple[str, ...]`; `ConfigService.read() -> dict` with keys `base` (dict of present base sections), `profiles` (dict), `local` (dict), `secrets` (dict mapping each `token_env` name → `{"set": bool, "source": str | None}`). A missing config file yields all-empty sections (onboarding).

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_service.py`:

```python
import herdeck.secrets as secrets
from herdeck.deckapp.config_service import ConfigService

CONFIG = """
[[servers]]
id = "local"
url = "ws://x"
token_env = "TOK"

[deck]
grid = "5x3"

[view]
management = "launcher_menu"

[notifications]
enabled = true
[notifications.telegram]
token_env = "TG"
chat_id = "42"

[profiles.mobile]
servers = ["local"]
[profiles.mobile.view]
management = "bottom_row"
"""


def _svc(tmp_path, text=CONFIG, local=None):
    (tmp_path / "config.toml").write_text(text)
    if local is not None:
        (tmp_path / "local.toml").write_text(local)
    return ConfigService(tmp_path / "config.toml", tmp_path / "local.toml")


def test_read_returns_base_profiles_local(tmp_path, monkeypatch):
    monkeypatch.delenv("TOK", raising=False)
    monkeypatch.delenv("TG", raising=False)
    svc = _svc(tmp_path, local='active_profile = "mobile"\n[hardware]\nbrightness = 70\n')
    data = svc.read()
    assert data["base"]["deck"] == {"grid": "5x3"}
    assert data["base"]["view"] == {"management": "launcher_menu"}
    assert data["profiles"]["mobile"]["view"] == {"management": "bottom_row"}
    assert data["local"]["active_profile"] == "mobile"
    assert data["local"]["hardware"]["brightness"] == 70


def test_read_redacts_secrets_to_presence_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "real")
    monkeypatch.delenv("TG", raising=False)
    svc = _svc(tmp_path)
    data = svc.read()
    # No secret VALUE appears anywhere in the payload.
    assert "real" not in repr(data)
    assert data["secrets"]["TOK"] == {"set": True, "source": "env"}
    assert data["secrets"]["TG"] == {"set": False, "source": None}


def test_read_missing_config_is_empty_for_onboarding(tmp_path):
    svc = ConfigService(tmp_path / "config.toml", tmp_path / "local.toml")
    assert svc.read() == {"base": {}, "profiles": {}, "local": {}, "secrets": {}}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config_service.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'herdeck.deckapp.config_service'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/herdeck/deckapp/config_service.py`:

```python
"""GUI-facing config service: read/validate/write the unified TOML config plus
profile and keychain-secret management. A thin layer over `settings`/`config` —
no config resolution logic is reimplemented here.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from .. import secrets as secret_store


class ConfigService:
    BASE_SECTIONS = (
        "servers",
        "deck",
        "answer_profiles",
        "macros",
        "start_profiles",
        "notifications",
        "theme",
        "view",
        "safety",
    )

    def __init__(self, config_path, local_path):
        self._config_path = Path(config_path)
        self._local_path = Path(local_path)

    def read(self) -> dict:
        if not self._config_path.exists():
            return {"base": {}, "profiles": {}, "local": {}, "secrets": {}}
        data = tomllib.loads(self._config_path.read_text())
        local = (
            tomllib.loads(self._local_path.read_text())
            if self._local_path.exists()
            else {}
        )
        base = {sec: data[sec] for sec in self.BASE_SECTIONS if sec in data}
        profiles = data.get("profiles", {})
        return {
            "base": base,
            "profiles": profiles,
            "local": local,
            "secrets": self._secret_flags(base),
        }

    def _secret_flags(self, base: dict) -> dict:
        names: list[str] = []
        for server in base.get("servers", []):
            env = server.get("token_env")
            if env and env not in names:
                names.append(env)
        tg = base.get("notifications", {}).get("telegram", {})
        if isinstance(tg, dict) and tg.get("token_env") and tg["token_env"] not in names:
            names.append(tg["token_env"])
        return {
            name: {"set": secret_store.has_secret(name), "source": secret_store.secret_source(name)}
            for name in names
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config_service.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/deckapp/config_service.py tests/test_config_service.py
git commit -m "feat(config-service): read() returns redacted base/profiles/local/secrets"
```

---

### Task 3: `ConfigService.validate(data)` — validate proposed (unsaved) config

**Files:**
- Modify: `src/herdeck/deckapp/config_service.py`
- Test: `tests/test_config_service.py`

**Interfaces:**
- Consumes: `settings.SettingsSnapshot`, `settings.validate_settings`.
- Produces: `ConfigService.validate(data: dict) -> list[str]` — builds a `SettingsSnapshot` from the proposed `data` (`{base, profiles, local}`) WITHOUT touching disk, runs `validate_settings`, returns the error list. Also `ConfigService._snapshot_for(data)` (private) returning the in-memory `SettingsSnapshot`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config_service.py`:

```python
import pytest


def test_validate_flags_unknown_server_in_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "real")
    svc = _svc(tmp_path)
    data = {
        "base": {"servers": [{"id": "local", "url": "ws://x", "token_env": "TOK"}]},
        "profiles": {"mobile": {"servers": ["ghost"]}},
        "local": {},
    }
    errors = svc.validate(data)
    assert any("unknown server 'ghost'" in e for e in errors)


def test_validate_clean_config_has_no_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "real")
    svc = _svc(tmp_path)
    data = {
        "base": {"servers": [{"id": "local", "url": "ws://x", "token_env": "TOK"}]},
        "profiles": {},
        "local": {},
    }
    assert svc.validate(data) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config_service.py -k validate -q`
Expected: FAIL — `AttributeError: 'ConfigService' object has no attribute 'validate'`.

- [ ] **Step 3: Write minimal implementation**

Add to `config_service.py` (imports at top, methods on the class):

```python
import os  # add to the existing imports
from ..settings import SettingsSnapshot, validate_settings  # add
```

```python
    def _snapshot_for(self, data: dict) -> SettingsSnapshot:
        toml_data = dict(data.get("base", {}))
        profiles = data.get("profiles") or {}
        if profiles:
            toml_data["profiles"] = profiles
        return SettingsSnapshot(
            config_path=self._config_path,
            local_path=self._local_path,
            data=toml_data,
            local_data=data.get("local", {}) or {},
            env_profile=os.environ.get("HERDECK_PROFILE"),
        )

    def validate(self, data: dict) -> list[str]:
        return validate_settings(self._snapshot_for(data))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config_service.py -k validate -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/deckapp/config_service.py tests/test_config_service.py
git commit -m "feat(config-service): validate() checks proposed config via validate_settings"
```

---

### Task 4: `ConfigService.write(data)` — structural gate + atomic TOML write

**Files:**
- Modify: `src/herdeck/deckapp/config_service.py`
- Modify: `pyproject.toml` (add `tomli-w` to `deck` and `dev`)
- Test: `tests/test_config_service.py`

**Interfaces:**
- Consumes: `_snapshot_for`, `validate`; `tomli_w.dumps`.
- Produces: `ConfigService.write(data: dict) -> list[str]` — returns STRUCTURAL errors (validation errors that are NOT "secret not set"); when empty, atomically writes `config.toml` (base + profiles) and `local.toml` (the `local` dict) and returns `[]`. Header line `# Managed by herdeck-config — generated; manual comments are not preserved` precedes the config TOML. Also `ConfigService.HEADER` (the header string) and private `_atomic_write(path, text)`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config_service.py`:

```python
import tomllib as _tomllib


def test_write_round_trips_and_omits_secret_values(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "real")
    svc = _svc(tmp_path)
    data = {
        "base": {
            "servers": [{"id": "local", "url": "ws://x", "token_env": "TOK"}],
            "deck": {"grid": "4x3"},
        },
        "profiles": {"mobile": {"view": {"management": "bottom_row"}}},
        "local": {"active_profile": "mobile"},
    }
    assert svc.write(data) == []
    text = (tmp_path / "config.toml").read_text()
    assert text.startswith("# Managed by herdeck-config")
    assert "real" not in text  # secret value never written
    parsed = _tomllib.loads(text)
    assert parsed["deck"] == {"grid": "4x3"}
    assert parsed["profiles"]["mobile"]["view"] == {"management": "bottom_row"}
    assert _tomllib.loads((tmp_path / "local.toml").read_text())["active_profile"] == "mobile"


def test_write_blocks_on_structural_error_but_not_missing_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("TOK", raising=False)  # secret missing -> NOT a write blocker
    svc = _svc(tmp_path)
    ok_data = {
        "base": {"servers": [{"id": "local", "url": "ws://x", "token_env": "TOK"}]},
        "profiles": {},
        "local": {},
    }
    assert svc.write(ok_data) == []  # missing secret does not block the write
    bad_data = {
        "base": {"servers": [{"id": "local", "url": "ws://x", "token_env": "TOK"}]},
        "profiles": {"a": {"extends": "b"}, "b": {"extends": "a"}},  # cycle = structural
        "local": {},
    }
    errors = svc.write(bad_data)
    assert errors and any("cycle" in e for e in errors)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config_service.py -k write -q`
Expected: FAIL — `AttributeError: 'ConfigService' object has no attribute 'write'`.

- [ ] **Step 3: Write minimal implementation**

Add `tomli-w` to `pyproject.toml` `deck` and `dev` lists, then `python -m pip install -e '.[dev]' -q`.

Add to `config_service.py`:

```python
import os
import tomli_w  # add to imports
```

```python
    HEADER = "# Managed by herdeck-config — generated; manual comments are not preserved\n"

    def write(self, data: dict) -> list[str]:
        structural = [e for e in self.validate(data) if "is not set" not in e]
        if structural:
            return structural
        toml_data = dict(data.get("base", {}))
        profiles = data.get("profiles") or {}
        if profiles:
            toml_data["profiles"] = profiles
        self._atomic_write(self._config_path, self.HEADER + tomli_w.dumps(toml_data))
        local = data.get("local") or {}
        self._atomic_write(self._local_path, tomli_w.dumps(local) if local else "")
        return []

    def _atomic_write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text)
        os.replace(tmp, path)
```

(`os` is already imported from Task 3.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config_service.py -k write -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/deckapp/config_service.py pyproject.toml tests/test_config_service.py
git commit -m "feat(config-service): write() — structural gate + atomic tomli-w write with header"
```

---

### Task 5: `ConfigService` profile + secret management

**Files:**
- Modify: `src/herdeck/deckapp/config_service.py`
- Test: `tests/test_config_service.py`

**Interfaces:**
- Consumes: `settings.load_settings`, `settings.set_active_profile`, `secrets.set_secret`/`clear_secret`.
- Produces:
  - `ConfigService.set_active(name: str) -> bool` — persists the active profile to `local.toml` via `set_active_profile` (respects env-lock → False; `"default"` valid). Reads the on-disk snapshot itself.
  - `ConfigService.create_profile(data: dict, name: str) -> dict` — returns a NEW `data` dict with an empty `[profiles.name]` added; raises `ConfigError` if `name == "default"` or already present.
  - `ConfigService.delete_profile(data: dict, name: str) -> dict` — returns a NEW `data` dict with `profiles[name]` removed; raises `ConfigError` if absent.
  - `ConfigService.set_secret(name, value)` / `clear_secret(name)` — delegate to the `secrets` store.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config_service.py`:

```python
from herdeck.config import ConfigError


def test_set_active_persists_and_respects_env_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "real")
    monkeypatch.delenv("HERDECK_PROFILE", raising=False)
    svc = _svc(tmp_path)
    assert svc.set_active("mobile") is True
    assert 'active_profile = "mobile"' in (tmp_path / "local.toml").read_text()
    monkeypatch.setenv("HERDECK_PROFILE", "mobile")
    assert svc.set_active("default") is False  # env-locked


def test_create_and_delete_profile_return_new_data(tmp_path):
    svc = _svc(tmp_path)
    data = {"base": {}, "profiles": {"mobile": {}}, "local": {}}
    created = svc.create_profile(data, "work")
    assert created["profiles"]["work"] == {}
    assert "work" not in data["profiles"]  # original untouched
    with pytest.raises(ConfigError, match="default"):
        svc.create_profile(created, "default")
    removed = svc.delete_profile(created, "work")
    assert "work" not in removed["profiles"]
    with pytest.raises(ConfigError, match="unknown profile 'ghost'"):
        svc.delete_profile(created, "ghost")


def test_set_and_clear_secret_delegate_to_store(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(secrets, "set_secret", lambda n, v: calls.append(("set", n, v)))
    monkeypatch.setattr(secrets, "clear_secret", lambda n: calls.append(("clear", n)))
    svc = _svc(tmp_path)
    svc.set_secret("TOK", "v")
    svc.clear_secret("TOK")
    assert calls == [("set", "TOK", "v"), ("clear", "TOK")]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config_service.py -k "set_active or profile or secret_delegate" -q`
Expected: FAIL — `AttributeError: 'ConfigService' object has no attribute 'set_active'`.

- [ ] **Step 3: Write minimal implementation**

Add to `config_service.py`:

```python
import copy  # add to imports
from ..config import ConfigError  # add
from ..settings import load_settings, set_active_profile  # add to the settings import
```

```python
    def set_active(self, name: str) -> bool:
        snapshot = load_settings(self._config_path, self._local_path)
        return set_active_profile(snapshot, name)

    def create_profile(self, data: dict, name: str) -> dict:
        if name == "default":
            raise ConfigError("profile 'default' is reserved (it is the base config)")
        profiles = data.get("profiles") or {}
        if name in profiles:
            raise ConfigError(f"profile '{name}' already exists")
        out = copy.deepcopy(data)
        out.setdefault("profiles", {})[name] = {}
        return out

    def delete_profile(self, data: dict, name: str) -> dict:
        profiles = data.get("profiles") or {}
        if name not in profiles:
            raise ConfigError(f"unknown profile '{name}'")
        out = copy.deepcopy(data)
        del out["profiles"][name]
        return out

    def set_secret(self, name: str, value: str) -> None:
        secret_store.set_secret(name, value)

    def clear_secret(self, name: str) -> None:
        secret_store.clear_secret(name)
```

Note: the secret-delegate test monkeypatches `herdeck.secrets.set_secret`/`clear_secret`; `config_service` calls them through the `secret_store` alias (`from .. import secrets as secret_store`), so the monkeypatch on the module attribute is observed.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config_service.py -q`
Expected: PASS (all config_service tests).

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/deckapp/config_service.py tests/test_config_service.py
git commit -m "feat(config-service): profile create/delete/set_active + secret set/clear"
```

---

### Task 6: Config HTTP routes on the sidecar + in-app reload

**Files:**
- Modify: `src/herdeck/deckapp/server.py`
- Test: `tests/test_deckapp.py`

**Interfaces:**
- Consumes: `ConfigService`, the existing `DeckApp` HTTP handler (`do_GET`/`do_POST`, `_require_query_token`/`_require_header_token`, `_send`).
- Produces: `DeckApp.__init__` gains an optional `config_service: ConfigService | None = None` and an optional `reloader: Callable[[], None] | None = None`; `DeckApp.reload()` rebuilds the source + re-renders. New routes:
  - `GET /config` → `config_service.read()` JSON (query-token).
  - `POST /config/validate` → body JSON `{base,profiles,local}` → `{"errors": [...]}` (header-token).
  - `POST /config` → on `write()` empty → `{"errors": []}` + `reload()`; else `{"errors": [...]}` (header-token).
  - `POST /profiles/active` → body `{"name": ...}` → `{"changed": bool}` (header-token).
  - `POST /secret` → body `{"token_env","value"}` → 204 (header-token).
  - `DELETE /secret/{token_env}` → 204 (header-token).
- `create_app`/`create_live_app`/`create_mock_app` pass a `ConfigService` built from the discovered config + local paths (a `reload()` no-op-safe default).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_deckapp.py` (follow the file's existing pattern for building a `DeckApp` on a real loopback port and issuing token-authed requests):

```python
import json
import urllib.request

from herdeck.deckapp.config_service import ConfigService
from herdeck.deckapp.server import create_mock_app


def _post(app, path, body, token=None):
    req = urllib.request.Request(
        f"http://{app.host}:{app.port}{path}",
        data=json.dumps(body).encode(),
        method="POST",
    )
    req.add_header("X-Herdeck-Token", token if token is not None else app.token)
    return urllib.request.urlopen(req)


def test_config_get_requires_token_and_returns_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "real")
    (tmp_path / "config.toml").write_text(
        '[[servers]]\nid="local"\nurl="ws://x"\ntoken_env="TOK"\n[deck]\ngrid="5x3"\n'
    )
    svc = ConfigService(tmp_path / "config.toml", tmp_path / "local.toml")
    app = create_mock_app(port=0, config_service=svc)
    try:
        # Wrong token -> 403
        bad = urllib.request.Request(f"http://{app.host}:{app.port}/config?token=nope")
        try:
            urllib.request.urlopen(bad)
            assert False, "expected 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
        # Right token -> redacted config
        ok = urllib.request.urlopen(f"http://{app.host}:{app.port}/config?token={app.token}")
        data = json.loads(ok.read())
        assert data["base"]["deck"] == {"grid": "5x3"}
        assert data["secrets"]["TOK"]["set"] is True
        assert "real" not in ok.headers.get("X-Debug", "")  # value never leaks
    finally:
        app.close()


def test_config_post_writes_and_triggers_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "real")
    cfg = tmp_path / "config.toml"
    cfg.write_text('[[servers]]\nid="local"\nurl="ws://x"\ntoken_env="TOK"\n[deck]\ngrid="5x3"\n')
    svc = ConfigService(cfg, tmp_path / "local.toml")
    reloaded = []
    app = create_mock_app(port=0, config_service=svc, reloader=lambda: reloaded.append(1))
    try:
        body = {"base": {"servers": [{"id": "local", "url": "ws://x", "token_env": "TOK"}],
                         "deck": {"grid": "4x3"}}, "profiles": {}, "local": {}}
        resp = _post(app, "/config", body, token=app.token)
        assert json.loads(resp.read())["errors"] == []
        assert reloaded == [1]
        assert 'grid = "4x3"' in cfg.read_text()
    finally:
        app.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_deckapp.py -k "config_get or config_post" -q`
Expected: FAIL — `create_mock_app() got an unexpected keyword argument 'config_service'`.

- [ ] **Step 3: Write minimal implementation**

In `server.py`, extend `DeckApp.__init__` signature with `config_service=None` and `reloader=None`, store them (`self._config_service = config_service`; `self._reloader = reloader`). Add:

```python
    def reload(self) -> None:
        """Re-pick the source from the (possibly changed) on-disk config and
        re-render, so the deck/preview reflects an edited config in place."""
        if self._reloader is not None:
            self._reloader()
```

In the handler's `do_GET`, add before the final `else`:

```python
                elif path == "/config":
                    if not self._require_query_token(url):
                        return
                    if app._config_service is None:
                        self._send(404)
                        return
                    self._send(200, json.dumps(app._config_service.read()).encode(),
                               "application/json")
```

In `do_POST`, add a JSON-body helper and routes:

```python
            def _json_body(self):
                length = int(self.headers.get("Content-Length", 0))
                return json.loads(self.rfile.read(length) or b"{}")

            ...
                elif path == "/config/validate":
                    if not self._require_header_token():
                        return
                    errors = app._config_service.validate(self._json_body())
                    self._send(200, json.dumps({"errors": errors}).encode(), "application/json")
                elif path == "/config":
                    if not self._require_header_token():
                        return
                    errors = app._config_service.write(self._json_body())
                    if not errors:
                        app.reload()
                    self._send(200, json.dumps({"errors": errors}).encode(), "application/json")
                elif path == "/profiles/active":
                    if not self._require_header_token():
                        return
                    changed = app._config_service.set_active(self._json_body().get("name"))
                    self._send(200, json.dumps({"changed": changed}).encode(), "application/json")
                elif path == "/secret":
                    if not self._require_header_token():
                        return
                    b = self._json_body()
                    app._config_service.set_secret(b["token_env"], b["value"])
                    self._send(204)
```

In `do_DELETE` (add the method to the handler):

```python
            def do_DELETE(self):
                path = urlsplit(self.path).path
                if path.startswith("/secret/"):
                    if not self._require_header_token():
                        return
                    app._config_service.clear_secret(path.rsplit("/", 1)[1])
                    self._send(204)
                else:
                    self._send(404)
```

Thread `config_service` and `reloader` through `create_mock_app`, `create_live_app`, and `create_app` (add the kwargs, default `None`). In `create_app`, build a default `ConfigService` from the discovered paths so the real sidecar always has one:

```python
def _default_config_service():
    from ..bootstrap import _discover_config_path, _discover_local_config_path
    from .config_service import ConfigService

    path = _discover_config_path() or os.path.expanduser("~/.config/herdeck/config.toml")
    return ConfigService(path, _discover_local_config_path(path))
```

(Use it as the default in `create_app` when no `config_service` is passed.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_deckapp.py -k "config_get or config_post" -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the deckapp suite (no regressions to existing deck routes)**

Run: `python -m pytest tests/test_deckapp.py tests/test_deckapp_live.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/deckapp/server.py tests/test_deckapp.py
git commit -m "feat(deckapp): config HTTP routes (read/validate/write/profiles/secret) + reload hook"
```

---

### Task 7: `ConfigWatcher` + sidecar in-app reload wiring

**Files:**
- Create: `src/herdeck/deckapp/watcher.py`
- Modify: `src/herdeck/deckapp/server.py` (`create_app` builds a watcher whose callback is the sidecar `reload`)
- Test: `tests/test_config_watcher.py`

**Interfaces:**
- Produces: `ConfigWatcher(paths: list[str | Path], on_change: Callable[[], None], *, interval: float = 1.0, clock=time.monotonic)`; `.start()`; `.close(timeout=2.0)`; fires `on_change` once per detected mtime change across any watched path (a fresh write of multiple files within one interval coalesces to a single call). Daemon thread; exceptions in `on_change` are swallowed.
- `create_app(..., reloader=...)`: when serving a real app, a `ConfigWatcher` over `[config_path, local_path]` calls `DeckApp.reload`; closing the app stops the watcher.

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_watcher.py`:

```python
import time

from herdeck.deckapp.watcher import ConfigWatcher


def test_watcher_fires_on_change_and_is_quiet_otherwise(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text("a = 1\n")
    calls = []
    w = ConfigWatcher([f], lambda: calls.append(1), interval=0.02)
    w.start()
    try:
        time.sleep(0.1)
        assert calls == []  # no change -> no fire
        f.write_text("a = 2\n")
        deadline = time.monotonic() + 1.0
        while not calls and time.monotonic() < deadline:
            time.sleep(0.02)
        assert calls == [1]
    finally:
        w.close()


def test_watcher_swallows_callback_errors(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text("a = 1\n")

    def boom():
        raise RuntimeError("nope")

    w = ConfigWatcher([f], boom, interval=0.02)
    w.start()
    try:
        f.write_text("a = 2\n")
        time.sleep(0.2)  # must not crash the daemon thread
        assert w._thread.is_alive()
    finally:
        w.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config_watcher.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'herdeck.deckapp.watcher'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/herdeck/deckapp/watcher.py`:

```python
"""Poll config file mtimes and fire a callback on change.

Drives both the sidecar's in-app reload and app.py's standalone hot-reload. A
poll (not an OS watch) keeps it dependency-free and cross-platform; the interval
is short enough for an interactive editor and cheap enough to ignore.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path


class ConfigWatcher:
    def __init__(self, paths, on_change: Callable[[], None], *, interval: float = 1.0,
                 clock=time.monotonic):
        self._paths = [Path(p) for p in paths]
        self._on_change = on_change
        self._interval = interval
        self._clock = clock
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="herdeck-config-watch", daemon=True)
        self._last = self._snapshot()

    def _snapshot(self) -> dict:
        out = {}
        for p in self._paths:
            try:
                out[p] = p.stat().st_mtime_ns
            except OSError:
                out[p] = None
        return out

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            current = self._snapshot()
            if current != self._last:
                self._last = current
                try:
                    self._on_change()
                except Exception:
                    pass

    def close(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._thread.join(timeout)
```

In `server.py` `create_app`, after building the serving app with a real `ConfigService`, start a watcher over the two paths whose callback is `app.reload`, and stop it in `DeckApp.close`. Minimal wiring: have `create_app` attach the watcher to the app instance (`app._watcher = ConfigWatcher([cfg, local], app.reload, interval=1.0); app._watcher.start()`) and extend `DeckApp.close` to `self._watcher.close()` when present (guard `getattr(self, "_watcher", None)`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config_watcher.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/deckapp/watcher.py src/herdeck/deckapp/server.py tests/test_config_watcher.py
git commit -m "feat(deckapp): ConfigWatcher + sidecar in-app reload on config change"
```

---

### Task 8: Standalone-deck hot-reload (B) — `app.py` re-resolves on config change

**Files:**
- Modify: `src/herdeck/app.py` (extract `_apply_config`; add `reload_from_disk`, `make_config_reloader`; wire a `ConfigWatcher` in `main()`)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: existing `App._handle_switch_profile` body (`update_config`, `_update_connectors`, `_build_notifier`, `_refresh`), `settings.load_settings`/`resolve_profile`, `deckapp.watcher.ConfigWatcher`.
- Produces:
  - `App._apply_config(new_config: Config) -> None` — the shared apply body (set config, rebuild notifier, `orch.update_config`, prune blocked keys, `_update_connectors`, clear restarted server state, `_refresh`).
  - `App.reload_from_disk() -> None` — if a `config_reloader` is set, call it to get the re-resolved active-profile `Config`, then `_apply_config`; on `ConfigError`, keep the current config and show a `"reload failed"` status panel.
  - `make_config_reloader(snapshot) -> Callable[[], Config]` — re-resolves the active profile from disk (`load_settings` + `resolve_profile(refreshed).config`), NO `set_active_profile`.
  - `App.__init__` gains `config_reloader: Callable[[], Config] | None = None`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_app.py` (reuse the file's existing `App` construction helpers; build an `App` with a stub `config_reloader` returning a new Config):

```python
def test_reload_from_disk_applies_new_config(make_app):
    # make_app: existing helper returning a constructed App on a known config.
    app = make_app()
    new_cfg = app.config  # start from current
    grids = []
    app._apply_config = lambda c: grids.append(c.grid)  # observe apply
    app._config_reloader = lambda: new_cfg
    app.reload_from_disk()
    assert grids == [new_cfg.grid]


def test_reload_from_disk_without_reloader_is_noop(make_app):
    app = make_app()
    app._config_reloader = None
    app.reload_from_disk()  # must not raise
```

(If `tests/test_app.py` has no `make_app` fixture, add a minimal one mirroring how the existing tests in that file build an `App`; the controller will confirm the exact constructor at implementation time.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_app.py -k reload_from_disk -q`
Expected: FAIL — `AttributeError: 'App' object has no attribute 'reload_from_disk'`.

- [ ] **Step 3: Write minimal implementation**

In `app.py`, refactor `_handle_switch_profile` to call a shared `_apply_config`, and add the reload entry points. Replace the apply tail of `_handle_switch_profile` (the block from `self.config = new_config` through `self._refresh()`) with `self._apply_config(new_config)`, and add:

```python
    def _apply_config(self, new_config: Config) -> None:
        self.config = new_config
        self.notifier = _build_notifier(new_config)
        self.orch.update_config(new_config)
        allowed_servers = {s.id for s in new_config.servers}
        self._blocked_keys = {k for k in self._blocked_keys if k.server_id in allowed_servers}
        restarted = set(self._update_connectors(new_config) or [])
        if restarted:
            self.orch.clear_server_state(restarted)
            self._blocked_keys = {k for k in self._blocked_keys if k.server_id not in restarted}
        for server_id in restarted:
            self.orch.set_connection(server_id, False)
        self._refresh()

    def reload_from_disk(self) -> None:
        if self._config_reloader is None:
            return
        try:
            new_config = self._config_reloader()
        except ConfigError as exc:
            self._refresh()
            self._set_status_panel("reload failed", [str(exc)[:60]], "amber")
            return
        self._apply_config(new_config)
```

Add `config_reloader=None` to `App.__init__` params and store `self._config_reloader = config_reloader`.

Add the reloader factory near `make_profile_switcher`:

```python
def make_config_reloader(snapshot):
    from .settings import load_settings, resolve_profile

    def reload_() -> Config:
        refreshed = load_settings(snapshot.config_path, snapshot.local_path)
        return resolve_profile(refreshed).config

    return reload_
```

In `main()`, where the file-backed `App` is built (the `switch_profile = make_profile_switcher(snapshot)` path, ~lines 660-705), also build `config_reloader = make_config_reloader(snapshot)`, pass it to the `App`, and start a `ConfigWatcher` over `[snapshot.config_path, snapshot.local_path]` whose callback marshals the reload onto the event loop the same way the ticker does:

```python
        from .deckapp.watcher import ConfigWatcher

        config_reloader = make_config_reloader(snapshot)
        # ... pass config_reloader=config_reloader into the App(...) construction ...
        watcher = ConfigWatcher(
            [snapshot.config_path, snapshot.local_path],
            lambda: loop.call_soon_threadsafe(app.reload_from_disk),
        )
        watcher.start()
```

(Use the same `loop` variable the existing `_ticker`/`call_soon_threadsafe` wiring uses; close `watcher` alongside the existing shutdown path.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_app.py -k reload_from_disk -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the full suite + lint**

Run: `PYTHONPATH=. python -m pytest -q && ruff check src/herdeck && ruff format --check src/herdeck/secrets.py src/herdeck/deckapp/config_service.py src/herdeck/deckapp/watcher.py`
Expected: all pass. (`ruff format --check src/herdeck` may flag the 8 pre-existing unrelated files — only the new files must be clean.)

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/app.py tests/test_app.py
git commit -m "feat(app): standalone-deck config hot-reload via ConfigWatcher (re-resolve active profile)"
```

---

## Self-Review

**1. Spec coverage:**
- ConfigService read/validate/write → Tasks 2–4. ✓
- Profile management (list via read/`profile_names`; set_active; create/delete) → Task 5. ✓
- Keychain secrets (set/clear/has/source, env-first) + core `_server_config` reads it → Task 1 + Task 5. ✓
- HTTP routes (config get/validate/write, profiles/active, secret) with token-auth → Task 6. ✓
- In-app reload (A) → Task 6 (`reload`) + Task 7 (watcher drives it). ✓
- File-watch hot-reload (B) for standalone deck → Task 8. ✓
- Atomic `tomli-w` write + header + secret never in TOML/HTTP → Task 4 + Task 6 tests. ✓
- Onboarding (missing config → empty read) → Task 2. ✓
- Deps `keyring`/`tomli-w` → Task 1 / Task 4. ✓
- NOTE: the spec text says `_server_config` lives in `config.py`; it is actually in `settings.py` — the plan targets `settings.py` (correct).

**2. Placeholder scan:** Every code step shows real code. The two soft spots (Task 6 says "follow the file's existing pattern" for building the loopback request; Task 8 says "the controller will confirm the exact `App` constructor / `make_app` fixture") are because `tests/test_deckapp.py` and `tests/test_app.py` already contain those harnesses — the implementer reuses them rather than inventing new ones. No "TBD"/"add error handling"/missing-code steps.

**3. Type consistency:** `ConfigService` method names (`read`, `validate`, `write`, `set_active`, `create_profile`, `delete_profile`, `set_secret`, `clear_secret`) are used identically in Tasks 2–6. `secrets` API (`get_secret`/`set_secret`/`clear_secret`/`has_secret`/`secret_source`) is consistent across Tasks 1, 2, 5. `DeckApp.reload`/`reloader`, `ConfigWatcher(paths, on_change, interval=, clock=)`, and `App._apply_config`/`reload_from_disk`/`make_config_reloader` match across Tasks 6–8. The editor data dict shape `{base, profiles, local, secrets}` is consistent in Tasks 2–6.
