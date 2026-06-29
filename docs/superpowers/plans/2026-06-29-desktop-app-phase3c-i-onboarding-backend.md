# Phase 3c-i — Onboarding backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the deckapp sidecar the ability to connect to a herdr bridge at first run — a locally running herdr via an embedded bridge (zero config) or a remote herdr (URL + token) — exposed over two `/setup*` HTTP routes, so the frontend (3c-ii) can replace the silent mock with a guided connect.

**Architecture:** The sidecar gains a source-selection precedence (`select_source_kind`) over a persisted onboarding choice; a `LocalBridgeRunner` runs the existing `start_local_bridge` on its own asyncio loop/thread and the existing `build_live_source` connects a Connector over the loopback bridge; `GET /setup` reports first-run status and `POST /setup/connect` runs the local/remote/demo flow (remote is test-before-commit via a `probe_server`, secret-before-config, verified before reporting success). Two token-injecting Rust proxy commands expose the routes to the WebView.

**Tech Stack:** Python 3.12+ (stdlib `http.server`, `asyncio`, `websockets>=14`, `keyring`, `tomllib`/`tomli_w`), Rust/Tauri (loopback HTTP proxies), PyInstaller (frozen sidecar).

## Global Constraints

- **Source-selection precedence** (deckapp), in order: `HERDECK_MOCK`→mock(`mock_env`); choice `local`+socket exists→local; choice `local`+socket missing→mock(`local_unavailable`); choice `demo`→mock(`demo`); remote config (`[[servers]]`+resolvable token)→remote; else→mock(`first_run`). **An explicit local/demo marker outranks a remote config** (a remote connect clears the marker, so remote config ⇒ no marker ⇒ remote wins) — so a demo/local choice sticks across restart even with a remote config present.
- **Onboarding state** persisted at `<config_dir>/onboarding.toml` (`choice = "local"|"demo"`; absent = never onboarded). `<config_dir>` = parent of the discovered config path (honors `HERDECK_CONFIG`). Atomic write. `tomllib` read, `tomli_w` write. Remote is implied by a usable `config.toml` (no marker); a successful remote connect CLEARS any stale marker.
- **Remote connect = test-before-commit, build-before-persist:** probe → upsert payload in memory → `resolve_selected_server(payload)` confirms `servers[0].id == id` → build the live source with the real token (`dataclasses.replace`) — ALL before any mutation. Only then persist secret-then-config (roll the secret back if the write errors/raises) → swap. Any failure ⇒ `{ok:false}` with **no durable state** (no orphaned secret, no serverful-but-dead config), source/bridge unchanged, no false success.
- **Timeouts:** `probe_server` default 4 s; the `setup_connect` Rust proxy uses a dedicated ≈15 s timeout that covers the WHOLE remote transaction (probe + build + render + snapshots + write + swap), not just the probe, so it never fires mid-persist (NOT the shared 3 s `SIDECAR_TIMEOUT`); `setup_status` uses the default short timeout.
- **Security:** loopback-only bind; `/setup*` use the same constant-time token auth as every route (GET `?token=`, POST `X-Herdeck-Token`). The sidecar access token is injected Rust-side, never in JS. Secret token VALUES are one-way: into `set_secret`/the embedded auth header only; never returned, logged, or written to TOML (config stores only the `token_env` NAME). Keyring service literal `"herdeck"`. The embedded bridge binds `127.0.0.1` on an ephemeral port with a random in-memory token.
- **Frozen:** no new heavy dep (bridge/bootstrap/connector are stdlib + `websockets`, already bundled). Never drop `websockets`.
- **Test runners (exact):** pytest `.venv/bin/python -m pytest <file> -v`; lint `.venv/bin/ruff check src tests` (BOTH dirs); Rust `cd desktop/src-tauri && ~/.cargo/bin/cargo test`; frozen gate `bash desktop/scripts/build-sidecar.sh && bash desktop/scripts/smoke-sidecar.sh`.

---

## File Structure

- `src/herdeck/bootstrap.py` — **modify**: add `resolve_socket_path` (moved from `app.py`).
- `src/herdeck/app.py` — **modify**: `_resolve_socket_path` delegates to the shared helper (no behavior change).
- `src/herdeck/deckapp/onboarding.py` — **create**: persisted onboarding choice (read/write/clear).
- `src/herdeck/deckapp/local_bridge.py` — **create**: `LocalBridgeRunner`.
- `src/herdeck/deckapp/probe.py` — **create**: `probe_server` + `ProbeResult`.
- `src/herdeck/deckapp/server.py` — **modify**: `select_source_kind`, `_resolve_source_kind`, `_make_local_source`, local branch in `create_app`, `DeckApp._set_local_bridge`/`_setup_status`/close wiring, the `setup_connect` function, and the `/setup` + `/setup/connect` routes.
- `desktop/src-tauri/src/http.rs` — **modify**: `fetch_setup` + `post_setup_connect` proxy helpers (+ tests).
- `desktop/src-tauri/src/lib.rs` — **modify**: `setup_status`/`setup_connect` commands + `SETUP_CONNECT_TIMEOUT` + registration.
- `desktop/herdeck-deckapp.spec` — **modify**: hiddenimports for the local-bridge path.
- `desktop/scripts/smoke-sidecar.sh` — **modify**: import-reachability check for the local path.
- Tests: `tests/test_bootstrap_socket_path.py`, `tests/test_deckapp_onboarding.py`, `tests/test_deckapp_local_bridge.py`, `tests/test_deckapp_probe.py`, `tests/test_deckapp_source_kind.py`, `tests/test_deckapp_local_wiring.py`, `tests/test_deckapp_setup_routes.py`; `desktop/src-tauri/tests/http.rs` additions.

---

### Task 1: Factor `resolve_socket_path` into bootstrap

Move the herdr-socket resolution out of `app.py` (CLI-only) into `bootstrap.py` so the deckapp can reuse it. No behavior change for the CLI.

**Files:**
- Modify: `src/herdeck/bootstrap.py`
- Modify: `src/herdeck/app.py:834-838`
- Test: `tests/test_bootstrap_socket_path.py`

**Interfaces:**
- Produces: `bootstrap.resolve_socket_path(config=None, *, getenv=os.environ.get) -> str` — `HERDR_SOCKET` env → `config.hardware.herdr_socket` → `~/.config/herdr/herdr.sock` (expanded).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bootstrap_socket_path.py
import os

from herdeck.bootstrap import resolve_socket_path


def test_env_override_wins():
    assert resolve_socket_path(None, getenv={"HERDR_SOCKET": "/tmp/x.sock"}.get) == "/tmp/x.sock"


def test_default_when_unset():
    expected = os.path.expanduser("~/.config/herdr/herdr.sock")
    assert resolve_socket_path(None, getenv={}.get) == expected


def test_config_hardware_override():
    class _HW:
        herdr_socket = "~/custom/herdr.sock"

    class _Cfg:
        hardware = _HW()

    assert resolve_socket_path(_Cfg(), getenv={}.get) == os.path.expanduser("~/custom/herdr.sock")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bootstrap_socket_path.py -v`
Expected: FAIL with `ImportError: cannot import name 'resolve_socket_path'`.

- [ ] **Step 3: Add the helper to bootstrap.py**

Add to `src/herdeck/bootstrap.py` (after the imports):

```python
def resolve_socket_path(config=None, *, getenv=os.environ.get) -> str:
    """Resolve the herdr Unix socket path: HERDR_SOCKET env, else the config's
    hardware override, else the XDG default. Shared by the CLI and the deckapp."""
    raw = getenv("HERDR_SOCKET") or (
        config.hardware.herdr_socket if config and config.hardware.herdr_socket else None
    )
    return os.path.expanduser(raw or "~/.config/herdr/herdr.sock")
```

- [ ] **Step 4: Delegate the app.py copy to it**

In `src/herdeck/app.py`, replace the body of `_resolve_socket_path` (lines 834-838) with a delegation, and import the shared helper. Change:

```python
def _resolve_socket_path(config: Config | None, *, getenv=os.environ.get) -> str:
    raw = getenv("HERDR_SOCKET") or (
        config.hardware.herdr_socket if config and config.hardware.herdr_socket else None
    )
    return os.path.expanduser(raw or "~/.config/herdr/herdr.sock")
```

to:

```python
def _resolve_socket_path(config: Config | None, *, getenv=os.environ.get) -> str:
    from .bootstrap import resolve_socket_path

    return resolve_socket_path(config, getenv=getenv)
```

- [ ] **Step 5: Run the new test + the existing app socket-path coverage**

Run: `.venv/bin/python -m pytest tests/test_bootstrap_socket_path.py -v && .venv/bin/python -m pytest tests/ -k "socket_path or resolve_mode or local_mode" -q`
Expected: PASS (no regression in the CLI socket-path / local-mode tests).

- [ ] **Step 6: Lint + commit**

```bash
.venv/bin/ruff check src tests
git add src/herdeck/bootstrap.py src/herdeck/app.py tests/test_bootstrap_socket_path.py
git commit -m "refactor: share resolve_socket_path via bootstrap for the deckapp"
```

---

### Task 2: Onboarding choice persistence

A focused module for the persisted first-run choice.

**Files:**
- Create: `src/herdeck/deckapp/onboarding.py`
- Test: `tests/test_deckapp_onboarding.py`

**Interfaces:**
- Produces:
  - `onboarding.state_path(config_path: str | None) -> pathlib.Path` — `<dirname(config_path)>/onboarding.toml`, or `~/.config/herdeck/onboarding.toml` when `config_path` is None.
  - `onboarding.read_choice(config_path) -> str | None` — `"local"`/`"demo"`/`None` (absent or unreadable → None).
  - `onboarding.write_choice(config_path, choice: str) -> None` — atomic; raises `ValueError` for a choice outside `{"local","demo"}`.
  - `onboarding.clear_choice(config_path) -> None` — remove the marker (idempotent).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deckapp_onboarding.py
import pytest

from herdeck.deckapp import onboarding


def test_absent_is_none(tmp_path):
    cfg = str(tmp_path / "config.toml")
    assert onboarding.read_choice(cfg) is None


def test_round_trip_local_then_demo(tmp_path):
    cfg = str(tmp_path / "config.toml")
    onboarding.write_choice(cfg, "local")
    assert onboarding.read_choice(cfg) == "local"
    onboarding.write_choice(cfg, "demo")
    assert onboarding.read_choice(cfg) == "demo"
    # the marker lives next to the config, not at the config path itself
    assert (tmp_path / "onboarding.toml").exists()


def test_clear_is_idempotent(tmp_path):
    cfg = str(tmp_path / "config.toml")
    onboarding.clear_choice(cfg)  # absent: no error
    onboarding.write_choice(cfg, "local")
    onboarding.clear_choice(cfg)
    assert onboarding.read_choice(cfg) is None


def test_invalid_choice_rejected(tmp_path):
    cfg = str(tmp_path / "config.toml")
    with pytest.raises(ValueError):
        onboarding.write_choice(cfg, "remote")


def test_none_config_path_uses_xdg_default():
    # state_path(None) points under ~/.config/herdeck/
    assert onboarding.state_path(None).name == "onboarding.toml"
    assert "herdeck" in str(onboarding.state_path(None))
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_deckapp_onboarding.py -v`
Expected: FAIL with `ModuleNotFoundError: ... onboarding`.

- [ ] **Step 3: Implement onboarding.py**

```python
# src/herdeck/deckapp/onboarding.py
"""Persisted first-run onboarding choice. A tiny marker file next to the config:
`<config_dir>/onboarding.toml` with `choice = "local" | "demo"`. Absent = the user
has never onboarded. Remote is NOT recorded here — it is implied by a usable
config.toml; a successful remote connect clears this marker."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

import tomli_w

_VALID = ("local", "demo")


def state_path(config_path: str | None) -> Path:
    base = (
        Path(config_path).expanduser().parent
        if config_path
        else Path(os.path.expanduser("~/.config/herdeck"))
    )
    return base / "onboarding.toml"


def read_choice(config_path) -> str | None:
    path = state_path(config_path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    choice = data.get("choice")
    return choice if choice in _VALID else None


def write_choice(config_path, choice: str) -> None:
    if choice not in _VALID:
        raise ValueError(f"invalid onboarding choice {choice!r}; want one of {_VALID}")
    path = state_path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(tomli_w.dumps({"choice": choice}), encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def clear_choice(config_path) -> None:
    state_path(config_path).unlink(missing_ok=True)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_deckapp_onboarding.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check src tests
git add src/herdeck/deckapp/onboarding.py tests/test_deckapp_onboarding.py
git commit -m "feat(deckapp): persist first-run onboarding choice"
```

---

### Task 3: LocalBridgeRunner

Run the existing `start_local_bridge` on a dedicated asyncio loop/thread so the embedded herdr bridge stays alive for the sidecar's lifetime, mirroring `live.ConnectorRunner`.

**Files:**
- Create: `src/herdeck/deckapp/local_bridge.py`
- Test: `tests/test_deckapp_local_bridge.py`

**Interfaces:**
- Consumes: `herdeck.bridge.start_local_bridge(socket_path, host="127.0.0.1", herdr=None) -> (host, port, token, (server, btask))`.
- Produces: `LocalBridgeRunner(socket_path, *, start_bridge=start_local_bridge)` with `start() -> (host, port, token)` (blocks until bound) and `close() -> None` (idempotent teardown).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deckapp_local_bridge.py
import asyncio
import functools

import websockets

from herdeck.bridge import StubHerdr, start_local_bridge
from herdeck.deckapp.local_bridge import LocalBridgeRunner


def test_runner_binds_and_serves_a_snapshot():
    herdr = StubHerdr(panes=[])
    runner = LocalBridgeRunner(
        "unused.sock", start_bridge=functools.partial(start_local_bridge, herdr=herdr)
    )
    host, port, token = runner.start()
    try:
        assert host == "127.0.0.1" and isinstance(port, int) and port > 0 and token

        async def _client():
            async with websockets.connect(
                f"ws://{host}:{port}",
                additional_headers={"Authorization": f"Bearer {token}"},
            ) as ws:
                return await asyncio.wait_for(ws.recv(), timeout=3)

        first = asyncio.run(_client())
        assert "snapshot" in first
    finally:
        runner.close()


def test_close_is_idempotent():
    herdr = StubHerdr(panes=[])
    runner = LocalBridgeRunner(
        "unused.sock", start_bridge=functools.partial(start_local_bridge, herdr=herdr)
    )
    runner.start()
    runner.close()
    runner.close()  # no error on second close
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_deckapp_local_bridge.py -v`
Expected: FAIL with `ModuleNotFoundError: ... local_bridge`.

- [ ] **Step 3: Implement local_bridge.py**

```python
# src/herdeck/deckapp/local_bridge.py
"""LocalBridgeRunner — owns the embedded herdr bridge on its own asyncio loop.

The bridge (start_local_bridge: a loopback WebSocket server reading the herdr Unix
socket) and the deckapp's Connector run on two SEPARATE loops/threads, talking only
over the loopback WebSocket. This runner mirrors live.ConnectorRunner: start the
bridge, block until bound, keep the loop running to serve it, and tear it down on
close()."""
from __future__ import annotations

import asyncio
import threading

from ..bridge import start_local_bridge


class LocalBridgeRunner:
    def __init__(self, socket_path: str, *, start_bridge=start_local_bridge):
        self._socket_path = socket_path
        self._start_bridge = start_bridge
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._serve, name="herdeck-local-bridge", daemon=True
        )
        self._ready = threading.Event()
        self._bound: tuple[str, int, str] | None = None
        self._handle = None  # (server, btask)
        self._error: BaseException | None = None

    def start(self) -> tuple[str, int, str]:
        self._thread.start()
        self._ready.wait(timeout=10)
        if self._error is not None:
            raise self._error
        if self._bound is None:
            raise RuntimeError("local bridge did not bind within 10s")
        return self._bound

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            host, port, token, handle = self._loop.run_until_complete(
                self._start_bridge(self._socket_path)
            )
            self._bound = (host, port, token)
            self._handle = handle
        except BaseException as exc:  # surface to start()
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()  # keep serving the bound bridge + broadcast task

    def close(self) -> None:
        loop = self._loop
        if loop.is_closed():
            return
        handle = self._handle

        async def _shutdown():
            if handle is not None:
                server, btask = handle
                btask.cancel()
                server.close()
                try:
                    await server.wait_closed()
                except Exception:
                    pass

        try:
            asyncio.run_coroutine_threadsafe(_shutdown(), loop).result(timeout=2)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        if self._thread.is_alive():
            self._thread.join(timeout=2)
        try:
            loop.close()
        except Exception:
            pass
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_deckapp_local_bridge.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check src tests
git add src/herdeck/deckapp/local_bridge.py tests/test_deckapp_local_bridge.py
git commit -m "feat(deckapp): LocalBridgeRunner hosts the embedded herdr bridge"
```

---

### Task 4: probe_server (remote test-before-commit)

A one-shot async probe that classifies a remote bridge as reachable / bad-token / unreachable, with no full Connector spin-up.

**Files:**
- Create: `src/herdeck/deckapp/probe.py`
- Test: `tests/test_deckapp_probe.py`

**Interfaces:**
- Produces: `probe.ProbeResult(ok: bool, reason: str)` and `async probe.probe_server(url, token, *, timeout=4.0) -> ProbeResult` (`reason` ∈ `{"ok","bad_token","unreachable"}`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deckapp_probe.py
import asyncio
import functools

from herdeck.bridge import StubHerdr, start_local_bridge
from herdeck.deckapp.local_bridge import LocalBridgeRunner
from herdeck.deckapp.probe import probe_server


def _probe(url, token, **kw):
    return asyncio.run(probe_server(url, token, **kw))


def test_probe_ok_and_bad_token():
    runner = LocalBridgeRunner(
        "unused.sock", start_bridge=functools.partial(start_local_bridge, herdr=StubHerdr(panes=[]))
    )
    host, port, token = runner.start()
    try:
        url = f"ws://{host}:{port}"
        ok = _probe(url, token)
        assert ok.ok and ok.reason == "ok"
        bad = _probe(url, "wrong-token")
        assert not bad.ok and bad.reason == "bad_token"
    finally:
        runner.close()


def test_probe_unreachable():
    r = _probe("ws://127.0.0.1:1", "t", timeout=0.5)  # port 1: nothing listening
    assert not r.ok and r.reason == "unreachable"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_deckapp_probe.py -v`
Expected: FAIL with `ModuleNotFoundError: ... probe`.

- [ ] **Step 3: Implement probe.py**

```python
# src/herdeck/deckapp/probe.py
"""One-shot server probe for the remote onboarding flow (test-before-commit).

Connects ws(s)://… with the Authorization: Bearer <token> header the bridge
expects. A first frame (the snapshot) means the token was accepted -> ok. The
bridge closes 4401 for a bad token. Anything else (refused/timeout/bad url) ->
unreachable. No Connector/backoff; a single attempt."""
from __future__ import annotations

import asyncio
import dataclasses

import websockets


@dataclasses.dataclass(frozen=True)
class ProbeResult:
    ok: bool
    reason: str  # "ok" | "bad_token" | "unreachable"


def _close_code(exc) -> int | None:
    rcvd = getattr(exc, "rcvd", None)
    return getattr(rcvd, "code", None) if rcvd is not None else getattr(exc, "code", None)


async def probe_server(url: str, token: str, *, timeout: float = 4.0) -> ProbeResult:
    try:
        async with asyncio.timeout(timeout):
            async with websockets.connect(
                url, additional_headers={"Authorization": f"Bearer {token}"}
            ) as ws:
                await ws.recv()  # first frame = snapshot -> token accepted
        return ProbeResult(True, "ok")
    except websockets.ConnectionClosed as exc:
        return ProbeResult(False, "bad_token" if _close_code(exc) == 4401 else "unreachable")
    except (OSError, asyncio.TimeoutError, websockets.InvalidURI, websockets.InvalidHandshake):
        return ProbeResult(False, "unreachable")
    except Exception:
        return ProbeResult(False, "unreachable")
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_deckapp_probe.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check src tests
git add src/herdeck/deckapp/probe.py tests/test_deckapp_probe.py
git commit -m "feat(deckapp): probe_server for remote test-before-commit"
```

---

### Task 5: select_source_kind (pure precedence)

The pure decision over already-gathered facts, plus a thin IO wrapper.

**Files:**
- Modify: `src/herdeck/deckapp/server.py` (add the two functions near `select_live`)
- Test: `tests/test_deckapp_source_kind.py`

**Interfaces:**
- Consumes: `select_live() -> (config, server) | None`; `onboarding.read_choice`; `bootstrap.resolve_socket_path`.
- Produces: `select_source_kind(*, mock_env, remote, choice, socket_path, socket_exists)` returning one of `("remote", config, server)`, `("local", socket_path)`, `("mock", reason)` (reason ∈ `{"mock_env","demo","first_run","local_unavailable"}`); and `_resolve_source_kind()` that gathers the facts and calls it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deckapp_source_kind.py
from herdeck.deckapp.server import select_source_kind

REMOTE = ("CONFIG", "SERVER")  # opaque sentinels; the function just passes them through


def k(**kw):
    base = dict(mock_env=False, remote=None, choice=None, socket_path="/s.sock", socket_exists=False)
    base.update(kw)
    return select_source_kind(**base)


def test_mock_env_wins():
    assert k(mock_env=True, remote=REMOTE, choice="local", socket_exists=True) == ("mock", "mock_env")


def test_remote_config():
    assert k(remote=REMOTE) == ("remote", "CONFIG", "SERVER")


def test_local_choice_overrides_remote_config():
    # an explicit local choice wins over a remote config on disk (sticks across restart)
    assert k(remote=REMOTE, choice="local", socket_exists=True) == ("local", "/s.sock")


def test_demo_choice_overrides_remote_config():
    assert k(remote=REMOTE, choice="demo") == ("mock", "demo")


def test_local_when_socket_present():
    assert k(choice="local", socket_exists=True) == ("local", "/s.sock")


def test_local_choice_but_socket_missing():
    assert k(choice="local", socket_exists=False) == ("mock", "local_unavailable")


def test_demo():
    assert k(choice="demo") == ("mock", "demo")


def test_first_run():
    assert k() == ("mock", "first_run")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_deckapp_source_kind.py -v`
Expected: FAIL with `ImportError: cannot import name 'select_source_kind'`.

- [ ] **Step 3: Implement the two functions**

Add to `src/herdeck/deckapp/server.py`, immediately after `select_live` (around line 499):

```python
def select_source_kind(*, mock_env, remote, choice, socket_path, socket_exists):
    """Pure source-selection precedence over already-gathered facts.

    Returns ("remote", config, server) | ("local", socket_path) | ("mock", reason).
    All IO (env, select_live result, persisted choice, socket existence) is passed
    in, so every branch is unit-testable without touching the filesystem."""
    if mock_env:
        return ("mock", "mock_env")
    # An explicit onboarding choice wins over a remote config on disk: a remote connect
    # CLEARS the marker, so a remote config always implies "no marker" and falls through to
    # the remote branch below. This makes a demo/local choice stick across restarts even
    # when a remote config.toml is present.
    if choice == "local":
        return ("local", socket_path) if socket_exists else ("mock", "local_unavailable")
    if choice == "demo":
        return ("mock", "demo")
    if remote is not None:
        config, server = remote
        return ("remote", config, server)
    return ("mock", "first_run")


def _resolve_source_kind():
    """Gather the facts and apply select_source_kind."""
    from ..bootstrap import resolve_socket_path
    from .onboarding import read_choice

    socket_path = resolve_socket_path(None)
    config_path = _default_config_paths()[0]
    return select_source_kind(
        mock_env=bool(os.environ.get("HERDECK_MOCK")),
        remote=select_live(),
        choice=read_choice(config_path),
        socket_path=socket_path,
        socket_exists=os.path.exists(socket_path),
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_deckapp_source_kind.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check src tests
git add src/herdeck/deckapp/server.py tests/test_deckapp_source_kind.py
git commit -m "feat(deckapp): select_source_kind precedence (mock/remote/local)"
```

---

### Task 6: Local source wiring + DeckApp bridge lifecycle

Build a live source over the embedded bridge, track the runner on the app, tear it down on close, and select it at startup.

**Files:**
- Modify: `src/herdeck/deckapp/server.py` (`DeckApp.__init__`/`close`, `_set_local_bridge`, `_make_local_source`, `create_app`, `_reloader_for`, `_select_source` made marker-aware)
- Test: `tests/test_deckapp_local_wiring.py`

**Interfaces:**
- Consumes: `LocalBridgeRunner`; `bootstrap.local_config`; `create_live_app`; `_resolve_source_kind` (Task 5).
- Produces:
  - `DeckApp._set_local_bridge(runner) -> None` — close any previous runner, store the new one (or `None` to drop the bridge).
  - `_start_local_bridge(socket_path, *, runner_factory=LocalBridgeRunner) -> (config, server, runner)` — start the embedded bridge and synthesize its loopback `(config, server)`; the caller owns `runner` teardown. Reused by `create_app` (startup local mode) and Task 8 (`/setup/connect` local).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deckapp_local_wiring.py
import functools
import types

from herdeck.bridge import StubHerdr, start_local_bridge
from herdeck.deckapp.local_bridge import LocalBridgeRunner
from herdeck.deckapp import server as srv


def test_reloader_is_noop_in_local_mode():
    calls = []
    fake_app = types.SimpleNamespace(swap_source=lambda s: calls.append(s))
    reload = srv._reloader_for(fake_app, ("local", "/s.sock"), lambda: "NEWSRC")
    reload()
    assert calls == []  # local reload must NOT swap (would orphan the bridge)


def test_reloader_swaps_in_mock_or_remote_mode():
    calls = []
    fake_app = types.SimpleNamespace(swap_source=lambda s: calls.append(s))
    reload = srv._reloader_for(fake_app, ("mock", "first_run"), lambda: "NEWSRC")
    reload()
    assert calls == ["NEWSRC"]


def _stub_runner_factory(socket_path):
    return LocalBridgeRunner(
        socket_path, start_bridge=functools.partial(start_local_bridge, herdr=StubHerdr(panes=[]))
    )


def test_start_local_bridge_yields_loopback_config_and_runner():
    config, server, runner = srv._start_local_bridge("unused.sock", runner_factory=_stub_runner_factory)
    try:
        assert server.url.startswith("ws://127.0.0.1:")
        assert server.token  # the in-memory bridge token
        assert config.servers == [server]
    finally:
        runner.close()


def test_set_local_bridge_closes_previous():
    app = srv.create_mock_app(serve=False)
    try:
        r1 = _stub_runner_factory("unused.sock")
        r1.start()
        app._set_local_bridge(r1)
        r2 = _stub_runner_factory("unused.sock")
        r2.start()
        app._set_local_bridge(r2)  # must close r1
        assert r1._loop.is_closed()
        assert not r2._loop.is_closed()
    finally:
        app._set_local_bridge(None)
        app.close()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_deckapp_local_wiring.py -v`
Expected: FAIL with `AttributeError: ... _start_local_bridge` (and `_set_local_bridge`).

- [ ] **Step 3: Add the lifecycle to DeckApp.__init__ and close**

In `DeckApp.__init__` (server.py), after `self._reloader = reloader` (line 73), add:

```python
        self._local_bridge = None  # LocalBridgeRunner when in local mode, else None
```

In `DeckApp.close` (server.py:163), after the `watcher` teardown block and before `self._source.close()`, add:

```python
        bridge = getattr(self, "_local_bridge", None)
        if bridge is not None:
            try:
                bridge.close()
            except Exception:
                pass
            self._local_bridge = None
```

Add the `_set_local_bridge` method to `DeckApp` (after `close`):

```python
    def _set_local_bridge(self, runner) -> None:
        """Adopt `runner` as the embedded-bridge owner, closing any previous one.
        Pass None to drop the bridge (e.g. when switching to remote/mock)."""
        old = getattr(self, "_local_bridge", None)
        if old is not None and old is not runner:
            try:
                old.close()
            except Exception:
                pass
        self._local_bridge = runner
```

- [ ] **Step 4: Add the `_start_local_bridge` helper + local branch in create_app**

Add near `create_app` in server.py:

```python
def _start_local_bridge(socket_path, *, runner_factory=None):
    """Start the embedded bridge and synthesize its loopback (config, server).
    Returns (config, server, runner); the caller owns runner teardown."""
    from ..bootstrap import local_config
    from .local_bridge import LocalBridgeRunner

    runner = (runner_factory or LocalBridgeRunner)(socket_path)
    try:
        _host, port, token = runner.start()
    except Exception:
        runner.close()  # clean up a partially-started runner before re-raising
        raise
    config = local_config(port, token)
    return config, config.servers[0], runner


def _reloader_for(app, kind, select_source):
    """The config-watch reloader for the built source. In LOCAL mode the embedded
    bridge's lifecycle is owned by create_app startup + /setup/connect, so a
    config-watch reload must be a no-op — re-selecting would swap the bridge source
    out (back to mock) and orphan the running LocalBridgeRunner."""
    if kind[0] == "local":
        return lambda: None
    return lambda: app.swap_source(select_source())
```

Make the reloader's source builder **marker-aware** by rewriting the existing
`_select_source()` (server.py:556) to go through `_resolve_source_kind()` instead of bare
`select_live()` — otherwise a demo/mock reload would ignore the onboarding marker and swap
to a resolvable remote config (contradicting "a demo/local choice sticks"). It is only ever
invoked by the NORMAL reloader (remote/demo/mock modes — local's reloader is a no-op), so
the `local` precedence result is treated defensively as mock (it must NOT start a bridge
here):

```python
def _select_source():
    """Re-select the source for a config-watch reload, RESPECTING the onboarding precedence
    (a demo/local marker is honored, not overridden by a resolvable remote config). Only the
    NORMAL reloader (remote/demo/mock) calls this; a `local` result is a defensive fallback
    to mock — the bridge is never (re)started from a reload."""
    kind = _resolve_source_kind()
    if kind[0] == "remote":
        from .live import build_live_source

        return build_live_source(kind[1], kind[2])
    from .mock import MockSource

    return MockSource()
```

In `create_app`, replace the source selection (server.py:588-605, the `selected = select_live()` ... `else: ... create_live_app(...)` block) with a kind-based build:

```python
    cfg_path, local_path = _default_config_paths()
    svc = config_service if config_service is not None else _default_config_service()
    kind = _resolve_source_kind()
    if kind[0] == "remote":
        _, config, server = kind
        app = create_live_app(
            config, server, host=host, port=port, icon_provider=icon_provider,
            serve=serve, config_service=svc,
        )
    elif kind[0] == "local":
        # create_live_app already builds with clock=time.monotonic, so live elapsed
        # time advances; the embedded bridge runner is tracked for teardown on close.
        _, socket_path = kind
        config, server, runner = _start_local_bridge(socket_path)
        try:
            app = create_live_app(
                config, server, host=host, port=port, icon_provider=icon_provider,
                serve=serve, config_service=svc,
            )
        except Exception:
            runner.close()  # don't leak the bridge thread/socket if app construction fails
            raise
        app._set_local_bridge(runner)
    else:
        app = create_mock_app(
            host=host, port=port, icon_provider=icon_provider, serve=serve, config_service=svc
        )
```

Then change the reloader-assignment block (server.py:606-609) to be kind-aware — in
local mode the config-watch reload must NOT swap the bridge source out. This runs at
STARTUP, so a deck launched from a persisted `local` choice gets the same no-op reloader
as a connect-time local switch (`kind = ("local", …)` → `_reloader_for` returns the no-op):

```python
    if reloader is None:
        app._reloader = _reloader_for(app, kind, _select_source)
    else:
        app._reloader = reloader
```

(The `ConfigWatcher` wiring below it stays unchanged — in local mode it still polls,
but `app.reload()` calls the no-op reloader, so a config-mtime change never orphans the
embedded bridge, whether local was entered at startup or via `/setup/connect`.
Local↔remote transitions go through `/setup/connect`.)

- [ ] **Step 5: Run to verify it passes (+ existing deckapp suite)**

Run: `.venv/bin/python -m pytest tests/test_deckapp_local_wiring.py tests/test_deckapp.py -v`
Expected: PASS (new wiring + no regression in the existing deckapp tests; mock/remote startup unchanged).

- [ ] **Step 6: Lint + commit**

```bash
.venv/bin/ruff check src tests
git add src/herdeck/deckapp/server.py tests/test_deckapp_local_wiring.py
git commit -m "feat(deckapp): local-mode source wiring + bridge lifecycle on DeckApp"
```

---

### Task 7: GET /setup status

Expose the first-run status the frontend renders against.

**Files:**
- Modify: `src/herdeck/deckapp/server.py` (`DeckApp._setup_status`, `/setup` route in `do_GET`)
- Test: `tests/test_deckapp_setup_routes.py` (status part)

**Interfaces:**
- Produces: `DeckApp._setup_status() -> dict` `{mode, connected, reason, local_herdr_available, choice, socket_path}`; `GET /setup?token=…` serves it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deckapp_setup_routes.py
import json
import os
import urllib.request

from herdeck.deckapp import server as srv


def _get(app, path):
    req = urllib.request.Request(f"http://{app.host}:{app.port}{path}")
    with urllib.request.urlopen(req, timeout=3) as r:
        return r.status, json.loads(r.read().decode())


def test_setup_status_first_run(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))  # absent -> first_run
    monkeypatch.setenv("HERDR_SOCKET", str(tmp_path / "nope.sock"))      # absent -> no local
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        status, body = _get(app, f"/setup?token={app.token}")
        assert status == 200
        assert body["mode"] == "mock"
        assert body["reason"] == "first_run"
        assert body["local_herdr_available"] is False
        assert body["choice"] is None
    finally:
        app.close()


def test_setup_status_demo_reason(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    cfg = str(tmp_path / "config.toml")
    monkeypatch.setenv("HERDECK_CONFIG", cfg)
    monkeypatch.setenv("HERDR_SOCKET", str(tmp_path / "nope.sock"))
    from herdeck.deckapp import onboarding

    onboarding.write_choice(cfg, "demo")
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        _, body = _get(app, f"/setup?token={app.token}")
        assert body["reason"] == "demo"
        assert body["choice"] == "demo"
    finally:
        app.close()


def test_setup_status_requires_token(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        import urllib.error

        try:
            _get(app, "/setup?token=wrong")
            assert False, "expected 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        app.close()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_deckapp_setup_routes.py -v`
Expected: FAIL (404 for `/setup`, no `_setup_status`).

- [ ] **Step 3: Implement `_setup_status` and the route**

Add the method to `DeckApp` (after `_health`, server.py:235):

```python
    def _setup_status(self) -> dict:
        from ..bootstrap import resolve_socket_path
        from .onboarding import read_choice

        socket_path = resolve_socket_path(None)
        socket_exists = os.path.exists(socket_path)
        config_path = str(self._config_service._config_path) if self._config_service else None
        choice = read_choice(config_path)
        live = self._source.source_name == "live"
        if live:
            mode = "local" if getattr(self, "_local_bridge", None) is not None else "remote"
            reason = None
        else:
            mode = "mock"
            if os.environ.get("HERDECK_MOCK"):
                reason = "mock_env"
            elif choice == "demo":
                reason = "demo"
            elif choice == "local" and not socket_exists:
                reason = "local_unavailable"
            else:
                reason = "first_run"
        return {
            "mode": mode,
            "connected": self._source.connected,
            "reason": reason,
            "local_herdr_available": socket_exists,
            "choice": choice,
            "socket_path": socket_path,
        }
```

In `do_GET` (server.py), add a branch alongside `/health` (before the final `else`):

```python
                elif path == "/setup":
                    if not self._require_query_token(url):
                        return
                    self._send(200, json.dumps(app._setup_status()).encode(), "application/json")
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_deckapp_setup_routes.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check src tests
git add src/herdeck/deckapp/server.py tests/test_deckapp_setup_routes.py
git commit -m "feat(deckapp): GET /setup first-run status"
```

---

### Task 8: POST /setup/connect

The runtime connect path: local / remote (test-before-commit, secret-before-config, verify) / demo.

**Files:**
- Modify: `src/herdeck/secrets.py` (`peek_keychain`)
- Modify: `src/herdeck/deckapp/watcher.py` (`ConfigWatcher.resync`)
- Modify: `src/herdeck/deckapp/config_service.py` (`resolve_selected_server`)
- Modify: `src/herdeck/deckapp/server.py` (`connect` + `_commit_remote` + `/setup/connect` route, `_token_env_for`, `_restore_secret`, `_restore_file`, `_restore_choice`, `DeckApp.reload` suppress guard, `_refresh_locked` split into `_render_locked` (fallible) + `_apply_rendered_locked` (pure assign), `DeckApp.swap_source` split into `_prepare_swap` (renders) + `_commit_swap` (assignment-only), `DeckApp._setup_lock` as the shared durable-state mutation lock held by `/setup/connect` AND the existing config-write routes (`/config`, `/profiles/active`, `/secret`, DELETE `/secret`); the marker is changed ONLY by `/setup/connect` (config routes never touch it), per-mode `app._reloader` update on connect; local/remote/demo connect are all fully transactional — prepare-before-persist, commit-after, full rollback)
- Test: `tests/test_deckapp_setup_routes.py` (connect part)

**Interfaces:**
- Consumes: `probe_server` (Task 4); `_start_local_bridge` (Task 6); `onboarding.write_choice/clear_choice` (Task 2); `secret_store.set_secret/clear_secret`; `ConfigService.read/write/resolve_selected_server`; `build_live_source_for_connect`.
- Produces:
  - `ConfigService.resolve_selected_server(data: dict) -> tuple[Config, ServerConfig] | None` — the resolved `(config, servers[0])` for the active profile, resolved in-memory with placeholder token envs (so a not-yet-stored secret doesn't block it); a connect caller replaces the chosen server's token with the real one (`dataclasses.replace`) to build the live source before persisting.
  - `secrets.peek_keychain(name: str) -> str | None` — the keychain value for `name` (keychain only, ignoring env), for snapshot/restore around `set_secret`.
  - `_token_env_for(server_id: str) -> str` — `HERDECK_<ID>_TOKEN` (upper, non-alnum→`_`).
  - `_restore_secret(name: str, prior: str | None) -> None` — restore the keychain to its snapshot: re-`set_secret(prior)` if it existed, else `clear_secret` (best-effort). So a rollback never destroys a pre-existing token when reconnecting an existing server.
  - `_restore_file(path, prior_text) -> None` — restore a file to its snapshot (or remove it if absent before), to undo a partial config write.
  - `_commit_remote(app, payload, token_env, token, new_source, config_path) -> dict` — the remote persist+swap transaction (secret-then-config, rollback on failure), run with the config watcher suppressed + resynced so its mtime poll can't reload mid-commit or after a rollback.
  - `ConfigWatcher.resync() -> None` — adopt the current file mtimes as the baseline (so an in-process writer's own change doesn't trigger a reload); `DeckApp._suppress_reload` (bool) gates `DeckApp.reload`.
  - `connect(app, body) -> dict | None` — returns the response dict, or `None` for a malformed body (route sends 400). Module-level so tests monkeypatch `srv._probe_sync` / `srv.build_live_source_for_connect`.
  - `POST /setup/connect` route.

**Ordering invariants (all enforced below):**
- **Build-before-persist** (remote): probe → upsert payload in memory → `resolve_selected_server` (placeholder tokens) confirms `servers[0].id == id` → **build the live source from the resolved config with the REAL token** (`dataclasses.replace(server, token=token)`) — all BEFORE any mutation. A failure at any of these steps persists **nothing** (no keychain entry, no config write). Only after the source is built do we persist **secret-then-config**; if the config write raises/returns errors, the just-set secret is rolled back (`_clear_secret_quiet`) and the freshly-built source is closed. So **any `{ok:false}` leaves no durable state** — no orphaned secret, no serverful config without a working connect.
- **Build → prepare → persist → commit → adopt** (both local + remote): build the new source, then `_prepare_swap` (build the orchestrator **and render the tiles** — all the fallible parts) **before** any durable write, then persist (secret/config/marker), then `_commit_swap` (**assignment-only** — assign the source/orchestrator/clock + the pre-rendered tiles under the lock, no render). So a config/orchestrator/render failure aborts BEFORE any durable mutation, and the post-persist commit is provably non-throwing (pure assignment) — no swap-rollback-of-durable-state path is ever needed. Every failure before the commit closes the just-built source (and the local bridge runner) and restores any snapshot (secret/config/marker), so its connector never leaks and no durable state survives a `{ok:false}`.
- **Honest `connected`:** the connect response's `connected` is `app._source.connected` at swap time (the connector dials asynchronously, so it is usually `false` immediately); the probe already proved reachability and the frontend's `/setup` poll surfaces the live status as it flips. Never hardcode `true`.
- **Serialized + reloader-coherent:** `app._setup_lock` is the shared durable-state mutation lock — `/setup/connect` holds it across the whole `connect()` flow AND every config-editor write route (`/config`, `/profiles/active`, `/secret`, DELETE `/secret`) holds it across its mutation, so a concurrent edit can't interleave with a connect's snapshot/write/rollback (ThreadingHTTPServer serves these concurrently). Each connect also updates `app._reloader` to match the NEW mode (`_reloader_for(app, kind, _select_source)`): **local → no-op** (a config-watch reload must not swap the adopted bridge source out and orphan its runner); **remote/demo/mock → normal** `_select_source` reload. The onboarding marker is the user's explicit *connection* choice, changed ONLY by `/setup/connect` — the config-editor routes never touch it, so a local/demo choice sticks across config edits (to switch to remote the user re-onboards).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deckapp_setup_routes.py  (append)
import urllib.error

from herdeck.deckapp.mock import MockSource
from herdeck.deckapp.probe import ProbeResult


class _DisconnectedSource(MockSource):
    """A MockSource reporting connected=False — proves /setup/connect returns the real
    source status (the connector dials asynchronously), not a hardcoded True."""

    @property
    def connected(self) -> bool:
        return False


def _post(app, path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://{app.host}:{app.port}{path}", data=data, method="POST",
        headers={"X-Herdeck-Token": app.token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read().decode())


class _FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def get_password(self, service, name):
        return self.store.get((service, name))

    def delete_password(self, service, name):
        self.store.pop((service, name), None)


def test_connect_demo_persists(tmp_path, monkeypatch):
    cfg = str(tmp_path / "config.toml")
    monkeypatch.setenv("HERDECK_CONFIG", cfg)
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        status, body = _post(app, "/setup/connect", {"choice": "demo"})
        assert status == 200 and body["ok"] is True
        from herdeck.deckapp import onboarding

        assert onboarding.read_choice(cfg) == "demo"
    finally:
        app.close()


def test_connect_demo_marker_write_failure_rolls_back(tmp_path, monkeypatch):
    from herdeck.deckapp import onboarding

    cfg = str(tmp_path / "config.toml")
    monkeypatch.setenv("HERDECK_CONFIG", cfg)
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    monkeypatch.setattr(onboarding, "write_choice", lambda cp, choice: (_ for _ in ()).throw(OSError("disk full")))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    prev = app._source
    try:
        _, body = _post(app, "/setup/connect", {"choice": "demo"})
        assert body["ok"] is False  # marker write failed before the commit
        assert app._source is prev  # swap not committed
        assert onboarding.read_choice(cfg) is None  # no marker persisted
    finally:
        app.close()


def test_config_write_does_not_touch_marker(tmp_path, monkeypatch):
    # The config editor edits content, not connection mode: a /config write must NOT change
    # the onboarding marker, so an explicit local/demo choice sticks across edits.
    from herdeck.deckapp import onboarding

    cfg = str(tmp_path / "config.toml")
    monkeypatch.setenv("HERDECK_CONFIG", cfg)
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    onboarding.write_choice(cfg, "demo")  # explicit demo choice
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        write_body = {
            "base": {"servers": [{"id": "herdr", "url": "ws://x:8788", "token_env": "HERDECK_HERDR_TOKEN"}]},
            "profiles": {},
            "local": {},
        }
        _, res = _post(app, "/config", write_body)
        assert res.get("errors") == []  # config written
        assert onboarding.read_choice(cfg) == "demo"  # marker untouched — demo choice sticks
    finally:
        app.close()


def test_connect_remote_writes_secret_then_config_and_verifies(tmp_path, monkeypatch):
    cfg = str(tmp_path / "config.toml")
    monkeypatch.setenv("HERDECK_CONFIG", cfg)
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    # Inject a probe that says ok + a remote source builder that does not touch the
    # network and reports connected=False (so we can assert the response is honest).
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda config, server: _DisconnectedSource())
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://10.0.0.5:8788", "token": "secret-tok", "id": "herdr"},
        )
        assert status == 200 and body["ok"] is True
        assert body["connected"] is False  # honest: the just-built source isn't connected yet
        # secret stored under the derived env name, NOT written to TOML
        assert fake.store[("herdeck", "HERDECK_HERDR_TOKEN")] == "secret-tok"
        text = (tmp_path / "config.toml").read_text()
        assert "HERDECK_HERDR_TOKEN" in text and "secret-tok" not in text
    finally:
        app.close()


def test_connect_remote_build_failure_leaves_previous_source_and_bridge(tmp_path, monkeypatch):
    import functools

    from herdeck.bridge import StubHerdr, start_local_bridge
    from herdeck.deckapp.local_bridge import LocalBridgeRunner

    cfg = str(tmp_path / "config.toml")
    monkeypatch.setenv("HERDECK_CONFIG", cfg)
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))

    def _boom(config, server):
        raise RuntimeError("connector blew up")

    monkeypatch.setattr(srv, "build_live_source_for_connect", _boom)
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    # Simulate already being in local mode: a live bridge runner is adopted pre-connect.
    runner = LocalBridgeRunner(
        "unused.sock", start_bridge=functools.partial(start_local_bridge, herdr=StubHerdr(panes=[]))
    )
    runner.start()
    app._set_local_bridge(runner)
    prev_source = app._source
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://10.0.0.5:8788", "token": "secret-tok", "id": "herdr"},
        )
        # Build runs BEFORE persist, so a build failure: honest {ok:false}, NOTHING
        # persisted, and the previous source AND local bridge are left fully intact.
        assert status == 200 and body["ok"] is False and "could not build" in body["error"]
        assert not (tmp_path / "config.toml").exists()  # config never written
        assert ("herdeck", "HERDECK_HERDR_TOKEN") not in fake.store  # secret never stored
        assert app._source is prev_source
        assert app._local_bridge is runner and not runner._loop.is_closed()
    finally:
        app._set_local_bridge(None)
        app.close()


def test_connect_remote_probe_fail_persists_nothing(tmp_path, monkeypatch):
    cfg = str(tmp_path / "config.toml")
    monkeypatch.setenv("HERDECK_CONFIG", cfg)
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(False, "bad_token"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://10.0.0.5:8788", "token": "x", "id": "herdr"},
        )
        assert status == 200 and body["ok"] is False and body["error"] == "bad_token"
        assert not (tmp_path / "config.toml").exists()
        assert fake.store == {}
    finally:
        app.close()


def test_connect_remote_selection_mismatch_persists_nothing(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    # Pre-existing config whose default-selected server is "other" (servers[0]); the
    # upserted "herdr" is appended, so the resolved selection stays "other" -> mismatch.
    cfg.write_text('[[servers]]\nid = "other"\nurl = "ws://1.2.3.4:8788"\ntoken_env = "OTHER_TOK"\n')
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    fake.store[("herdeck", "OTHER_TOK")] = "other-secret"  # other server resolves -> genuine mismatch
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://10.0.0.5:8788", "token": "secret-tok", "id": "herdr"},
        )
        assert status == 200 and body["ok"] is False and "resolve to this server" in body["error"]
        # nothing about herdr persisted: no new server, no keychain entry
        assert "herdr" not in cfg.read_text().lower()
        assert ("herdeck", "HERDECK_HERDR_TOKEN") not in fake.store
    finally:
        app.close()


def test_connect_remote_rejects_unloadable_other_token_missing(tmp_path, monkeypatch):
    # herdr is servers[0] (selected), but a second server's token is missing — so the
    # config would NOT load on restart. The preflight placeholders only the new token,
    # so the missing OTHER token fails resolution and onboarding rejects, persisting nothing.
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[[servers]]\nid = "herdr"\nurl = "ws://old:8788"\ntoken_env = "HERDECK_HERDR_TOKEN"\n'
        '[[servers]]\nid = "extra"\nurl = "ws://x:8788"\ntoken_env = "EXTRA_TOK"\n'
    )
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()  # EXTRA_TOK is not stored anywhere
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        _, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://new:8788", "token": "t", "id": "herdr"},
        )
        assert body["ok"] is False  # EXTRA_TOK won't resolve -> config unloadable -> rejected
        assert ("herdeck", "HERDECK_HERDR_TOKEN") not in fake.store  # nothing persisted
        assert "ws://new" not in cfg.read_text()  # config not rewritten with the new url
    finally:
        app.close()


def test_connect_remote_write_raises_rolls_back_secret(tmp_path, monkeypatch):
    cfg = str(tmp_path / "config.toml")
    monkeypatch.setenv("HERDECK_CONFIG", cfg)
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    # Build succeeds (no real network); the failure is injected at the config write.
    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda config, server: _DisconnectedSource())
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())

    def _boom_write(payload):  # atomic write faults AFTER the secret was stored
        raise OSError("disk full")

    monkeypatch.setattr(app._config_service, "write", _boom_write)
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://10.0.0.5:8788", "token": "t", "id": "herdr"},
        )
        assert status == 200 and body["ok"] is False
        # the orphaned keychain secret was rolled back (no half-commit on a disk fault)
        assert ("herdeck", "HERDECK_HERDR_TOKEN") not in fake.store
    finally:
        app.close()


def test_connect_remote_partial_write_restores_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda config, server: _DisconnectedSource())
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    svc = app._config_service
    real_atomic = svc._atomic_write
    calls = {"n": 0}

    def _flaky_atomic(path, text):  # config.toml write succeeds, local.toml write faults
        calls["n"] += 1
        if calls["n"] == 1:
            return real_atomic(path, text)
        raise OSError("local write failed")

    monkeypatch.setattr(svc, "_atomic_write", _flaky_atomic)
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://10.0.0.5:8788", "token": "t", "id": "herdr"},
        )
        assert status == 200 and body["ok"] is False
        # the partially-written config.toml is restored away (prior was absent), secret rolled back
        assert not cfg.exists()
        assert ("herdeck", "HERDECK_HERDR_TOKEN") not in fake.store
    finally:
        app.close()


def test_connect_remote_prepare_failure_closes_source(tmp_path, monkeypatch):
    # build_live_source starts a connector immediately; if _prepare_swap then raises, the
    # built source MUST be closed so the connector doesn't leak.
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    closed = {"v": False}

    class _BadPrepare(MockSource):
        def close(self):
            closed["v"] = True

        @property
        def config(self):  # makes _prepare_swap raise (it reads config.grid)
            raise ValueError("boom")

    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda c, s: _BadPrepare())
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        _, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://x:8788", "token": "t", "id": "herdr"},
        )
        assert body["ok"] is False and "could not build" in body["error"]
        assert closed["v"]  # built source closed on prepare failure (no connector leak)
        assert ("herdeck", "HERDECK_HERDR_TOKEN") not in fake.store
    finally:
        app.close()


def test_connect_remote_config_snapshot_failure_persists_nothing(tmp_path, monkeypatch):
    # A config read fault during the pre-mutation snapshot must abort BEFORE set_secret,
    # leaving no orphaned keychain entry AND closing the just-built source.
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    closed = {"v": False}

    class _RecSrc(_DisconnectedSource):
        def close(self):
            closed["v"] = True

    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda c, s: _RecSrc())

    def _boom_snapshot(svc):
        raise OSError("read fault")

    monkeypatch.setattr(srv, "_snapshot_config", _boom_snapshot)
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        _, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://x:8788", "token": "t", "id": "herdr"},
        )
        assert body["ok"] is False
        assert ("herdeck", "HERDECK_HERDR_TOKEN") not in fake.store  # snapshot failed before set_secret
        assert closed["v"]  # built source closed (no connector leak)
    finally:
        app.close()


def test_connect_remote_write_failure_restores_prior_secret(tmp_path, monkeypatch):
    # Reconnecting an EXISTING "herdr" server whose token was already stored: a write
    # failure must RESTORE the prior secret (and prior config), not destroy them.
    cfg = tmp_path / "config.toml"
    cfg.write_text('[[servers]]\nid = "herdr"\nurl = "ws://old:8788"\ntoken_env = "HERDECK_HERDR_TOKEN"\n')
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    fake.store[("herdeck", "HERDECK_HERDR_TOKEN")] = "old-tok"  # pre-existing keychain token
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda c, s: _DisconnectedSource())
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())

    def _boom_write(payload):
        raise OSError("disk full")

    monkeypatch.setattr(app._config_service, "write", _boom_write)
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://new:8788", "token": "new-tok", "id": "herdr"},
        )
        assert status == 200 and body["ok"] is False
        # the PRIOR secret + config are restored, not destroyed/overwritten
        assert fake.store[("herdeck", "HERDECK_HERDR_TOKEN")] == "old-tok"
        assert "ws://old" in cfg.read_text()
    finally:
        app.close()


def test_connect_remote_set_secret_failure_restores_prior(tmp_path, monkeypatch):
    # set_secret can partially overwrite then raise (flaky backend); restore the prior.
    cfg = tmp_path / "config.toml"
    cfg.write_text('[[servers]]\nid = "herdr"\nurl = "ws://old:8788"\ntoken_env = "HERDECK_HERDR_TOKEN"\n')
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    fake.store[("herdeck", "HERDECK_HERDR_TOKEN")] = "old-tok"

    def _bad_set(service, name, value):  # partial-overwrites, then raises only for the new value
        fake.store[(service, name)] = value
        if value == "new-tok":
            raise RuntimeError("keychain backend error")

    monkeypatch.setattr(fake, "set_password", _bad_set)
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda c, s: _DisconnectedSource())
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        _, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://new:8788", "token": "new-tok", "id": "herdr"},
        )
        assert body["ok"] is False and "store token" in body["error"]
        assert fake.store[("herdeck", "HERDECK_HERDR_TOKEN")] == "old-tok"  # restored after partial set
    finally:
        app.close()


def test_connect_remote_malformed_existing_config(tmp_path, monkeypatch):
    # An unparseable existing config.toml must surface as {ok:false}, not a 500.
    cfg = tmp_path / "config.toml"
    cfg.write_text("this is { not valid toml")
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://x:8788", "token": "t", "id": "herdr"},
        )
        assert status == 200 and body["ok"] is False and "unreadable" in body["error"]
        assert ("herdeck", "HERDECK_HERDR_TOKEN") not in fake.store  # nothing persisted
    finally:
        app.close()


def test_connect_remote_structurally_invalid_servers(tmp_path, monkeypatch):
    # Parseable TOML with a wrong-shaped servers value must be rejected, not 500.
    cfg = tmp_path / "config.toml"
    cfg.write_text('servers = ["bad"]\n')
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://x:8788", "token": "t", "id": "herdr"},
        )
        assert status == 200 and body["ok"] is False and "malformed" in body["error"]
        assert ("herdeck", "HERDECK_HERDR_TOKEN") not in fake.store
    finally:
        app.close()


def test_connect_remote_invalid_id_type_is_400(tmp_path, monkeypatch):
    # A non-string id (e.g. {"id": 123}) must be a 400, not a 500 from _token_env_for.
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        try:
            _post(app, "/setup/connect", {"choice": "remote", "url": "ws://x:8788", "token": "t", "id": 123})
            raise AssertionError("expected 400")
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        app.close()


def test_connect_remote_existing_server_missing_token_env(tmp_path, monkeypatch):
    # An existing server dict missing token_env is parseable + dict-shaped, but resolution
    # raises KeyError in _server_config — must surface as {ok:false}, not a 500.
    cfg = tmp_path / "config.toml"
    cfg.write_text('[deck]\ngrid = "5x3"\n[[servers]]\nid = "other"\nurl = "ws://o:8788"\n')
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://x:8788", "token": "t", "id": "herdr"},
        )
        assert status == 200 and body["ok"] is False  # resolve failed -> doesn't select -> {ok:false}
        assert ("herdeck", "HERDECK_HERDR_TOKEN") not in fake.store
    finally:
        app.close()


def test_connect_remote_rejects_env_token_collision(tmp_path, monkeypatch):
    # token_env already exported with a different value would shadow the keychain
    # (env-first resolution), so the persisted config wouldn't use the typed token.
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    monkeypatch.setenv("HERDECK_HERDR_TOKEN", "env-stale-token")
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://x:8788", "token": "typed-token", "id": "herdr"},
        )
        assert status == 200 and body["ok"] is False and "environment" in body["error"]
        assert not (tmp_path / "config.toml").exists()  # rejected before any persist
        assert ("herdeck", "HERDECK_HERDR_TOKEN") not in fake.store
    finally:
        app.close()


def test_connect_remote_rejects_token_env_collision_with_other_server(tmp_path, monkeypatch):
    # `foo-bar` and `foo_bar` derive the SAME HERDECK_FOO_BAR_TOKEN; connecting one must not
    # be allowed to overwrite the other server's keychain token.
    cfg = tmp_path / "config.toml"
    cfg.write_text('[[servers]]\nid = "foo_bar"\nurl = "ws://o:8788"\ntoken_env = "HERDECK_FOO_BAR_TOKEN"\n')
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        _, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://x:8788", "token": "t", "id": "foo-bar"},
        )
        assert body["ok"] is False and "already used elsewhere" in body["error"]
        assert ("herdeck", "HERDECK_FOO_BAR_TOKEN") not in fake.store  # nothing persisted
    finally:
        app.close()


def test_connect_remote_rejects_token_env_collision_with_notifications(tmp_path, monkeypatch):
    # token_env is a flat namespace: a derived HERDECK_HERDR_TOKEN must not overwrite a
    # secret already referenced by a non-server section (here a Telegram notification).
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[deck]\ngrid = "5x3"\n'
        '[notifications.telegram]\ntoken_env = "HERDECK_HERDR_TOKEN"\nchat_id = "1"\n'
    )
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        _, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://x:8788", "token": "t", "id": "herdr"},
        )
        assert body["ok"] is False and "already used elsewhere" in body["error"]
    finally:
        app.close()


def test_connect_remote_keychain_read_failure_aborts(tmp_path, monkeypatch):
    # If snapshotting the prior token fails (keychain backend error), abort BEFORE set_secret
    # rather than risk erasing an existing token on a later rollback.
    cfg = tmp_path / "config.toml"
    cfg.write_text('[[servers]]\nid = "herdr"\nurl = "ws://old:8788"\ntoken_env = "HERDECK_HERDR_TOKEN"\n')
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    fake.store[("herdeck", "HERDECK_HERDR_TOKEN")] = "old-tok"

    def _bad_get(service, name):  # keychain backend read error (NOT "missing")
        raise RuntimeError("keychain read error")

    monkeypatch.setattr(fake, "get_password", _bad_get)
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda c, s: _DisconnectedSource())
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        _, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://new:8788", "token": "new-tok", "id": "herdr"},
        )
        assert body["ok"] is False and "keychain" in body["error"]
        assert fake.store[("herdeck", "HERDECK_HERDR_TOKEN")] == "old-tok"  # not overwritten/erased
    finally:
        app.close()


def test_connect_remote_marker_clear_failure_rolls_back(tmp_path, monkeypatch):
    # clear_choice is part of the commit: if it faults, everything rolls back so a
    # later-removed remote config falls to first_run, never a stale masking marker.
    from herdeck.deckapp import onboarding

    cfg = tmp_path / "config.toml"
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    closed = {"v": False}

    class _RecSrc(_DisconnectedSource):
        def close(self):
            closed["v"] = True

    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda c, s: _RecSrc())
    monkeypatch.setattr(onboarding, "clear_choice", lambda cp: (_ for _ in ()).throw(OSError("nope")))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    prev = app._source
    try:
        status, body = _post(
            app, "/setup/connect",
            {"choice": "remote", "url": "ws://10.0.0.5:8788", "token": "t", "id": "herdr"},
        )
        assert status == 200 and body["ok"] is False
        # full rollback: config + secret gone, source unchanged, AND the built source closed
        assert not cfg.exists()
        assert ("herdeck", "HERDECK_HERDR_TOKEN") not in fake.store
        assert app._source is prev
        assert closed["v"]  # the just-built source was closed (no connector leak)
    finally:
        app.close()


def test_connect_local_write_choice_failure_closes_source_and_runner(tmp_path, monkeypatch):
    from herdeck.deckapp import onboarding

    sock = tmp_path / "herdr.sock"
    sock.touch()  # exists, so the socket check passes
    monkeypatch.setenv("HERDR_SOCKET", str(sock))
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    closed = {"source": False, "runner": False}

    class _RecSource(MockSource):
        def close(self):
            closed["source"] = True

    class _RecRunner:
        def close(self):
            closed["runner"] = True

    monkeypatch.setattr(srv, "_start_local_bridge", lambda sp: ("CFG", "SRV", _RecRunner()))
    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda c, s: _RecSource())
    monkeypatch.setattr(onboarding, "write_choice", lambda cp, choice: (_ for _ in ()).throw(OSError("disk full")))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    prev = app._source
    try:
        status, body = _post(app, "/setup/connect", {"choice": "local"})
        assert status == 200 and body["ok"] is False
        assert closed["source"] and closed["runner"]  # neither the built source nor bridge leaks
        assert app._source is prev and app._local_bridge is None  # previous state untouched
    finally:
        app.close()


def test_reload_is_suppressed_while_flag_set(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    app = srv.create_mock_app(serve=False)
    calls = []
    app._reloader = lambda: calls.append(1)
    try:
        app._suppress_reload = True
        app.reload()
        assert calls == []  # the watcher-driven reload is muted during the commit
        app._suppress_reload = False
        app.reload()
        assert calls == [1]  # and fires normally once the commit clears the flag
    finally:
        app.close()


def test_watcher_resync_adopts_current_mtimes(tmp_path):
    from herdeck.deckapp.watcher import ConfigWatcher

    p = tmp_path / "config.toml"
    p.write_text("a = 1\n")
    w = ConfigWatcher([p], lambda: None, interval=999)  # not started: no polling in the test
    p.write_text("a = 2\n")  # change after the constructor snapshotted
    w.resync()  # adopt the new mtime as baseline
    assert w._snapshot() == w._last  # a subsequent poll would see no change


def test_swap_source_bad_config_does_not_half_swap(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    app = srv.create_mock_app(serve=False)
    prev = app._source

    class _BadSource(MockSource):
        @property
        def config(self):
            raise ValueError("malformed config")

    try:
        try:
            app.swap_source(_BadSource())
            raise AssertionError("expected swap_source to raise")
        except ValueError:
            pass
        assert app._source is prev  # build-then-assign: a bad config never half-swaps
    finally:
        app.close()


def test_prepare_commit_swap_adopts_given_clock(tmp_path, monkeypatch):
    import time

    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    app = srv.create_mock_app(serve=False)  # mock clock is a fixed lambda: 0.0
    try:
        src = MockSource()
        app._commit_swap(src, app._prepare_swap(src, clock=time.monotonic))
        assert app._clock is time.monotonic  # the live clock is adopted, not the mock's frozen one
        assert app._source is src
    finally:
        app.close()


def test_concurrent_remote_connects_are_serialized(tmp_path, monkeypatch):
    import threading
    import tomllib

    cfg = tmp_path / "config.toml"
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "_probe_sync", lambda url, token: ProbeResult(True, "ok"))
    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda c, s: _DisconnectedSource())
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    results = []

    def _go():
        try:
            _, body = _post(
                app, "/setup/connect",
                {"choice": "remote", "url": "ws://x:8788", "token": "t", "id": "herdr"},
            )
            results.append(body)
        except Exception as exc:  # noqa: BLE001
            results.append(exc)

    threads = [threading.Thread(target=_go) for _ in range(2)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        # Both complete without error; the _setup_lock serialized them, so the config is
        # consistent: exactly one herdr server, not a half-interleaved write.
        assert len(results) == 2 and all(isinstance(r, dict) for r in results)
        data = tomllib.loads(cfg.read_text())
        assert sum(1 for s in data.get("servers", []) if s.get("id") == "herdr") == 1
    finally:
        app.close()


def test_config_write_serialized_with_connect_via_setup_lock(tmp_path, monkeypatch):
    # The shared _setup_lock must serialize a config-editor write against a /setup/connect:
    # while a connect holds the lock (blocked mid-transaction) a /config write cannot proceed,
    # and the final state stays coherent (no interleaved/half-written config).
    import threading
    import time as _t
    import tomllib

    cfg = tmp_path / "config.toml"
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda c, s: _DisconnectedSource())
    in_connect = threading.Event()
    release = threading.Event()

    def _blocking_probe(url, token):  # connect blocks here (holding _setup_lock), then fails
        in_connect.set()
        release.wait(5)
        return ProbeResult(False, "unreachable")

    monkeypatch.setattr(srv, "_probe_sync", _blocking_probe)
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    out = {}

    def _connect():
        _, out["connect"] = _post(
            app, "/setup/connect", {"choice": "remote", "url": "ws://x:8788", "token": "t", "id": "herdr"}
        )

    def _cfg():
        out["cfg"] = _post(
            app, "/config",
            {"base": {"servers": [{"id": "editor", "url": "ws://e:8788", "token_env": "HERDECK_EDITOR_TOKEN"}]},
             "profiles": {}, "local": {}},
        )

    ct, wt = threading.Thread(target=_connect), threading.Thread(target=_cfg)
    try:
        ct.start()
        assert in_connect.wait(5)  # connect now holds _setup_lock (blocked in the probe)
        wt.start()
        _t.sleep(0.2)  # give the config write time to reach + block on the lock
        assert "cfg" not in out  # it CANNOT complete while the connect holds the lock
        release.set()
        ct.join(10)
        wt.join(10)
        assert out["connect"]["ok"] is False  # connect failed (probe), nothing persisted
        _, cfg_body = out["cfg"]
        assert cfg_body.get("errors") == []  # config write succeeded once serialized
        ids = [s.get("id") for s in tomllib.loads(cfg.read_text()).get("servers", [])]
        assert ids == ["editor"]  # coherent: only the editor's server, no failed-connect leftover
    finally:
        release.set()
        app.close()


def test_connect_local_swap_failure_rolls_back(tmp_path, monkeypatch):
    from herdeck.deckapp import onboarding

    sock = tmp_path / "herdr.sock"
    sock.touch()
    monkeypatch.setenv("HERDR_SOCKET", str(sock))
    cfg = str(tmp_path / "config.toml")
    monkeypatch.setenv("HERDECK_CONFIG", cfg)
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    closed = {"source": False, "runner": False}

    class _Runner:
        def close(self):
            closed["runner"] = True

    class _BadSource(MockSource):
        def close(self):
            closed["source"] = True

        @property
        def config(self):  # makes _prepare_swap raise (it reads config.grid first)
            raise ValueError("malformed")

    monkeypatch.setattr(srv, "_start_local_bridge", lambda sp: ("CFG", "SRV", _Runner()))
    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda c, s: _BadSource())
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    prev = app._source
    try:
        _, body = _post(app, "/setup/connect", {"choice": "local"})
        assert body["ok"] is False
        assert app._source is prev  # prepare failed before commit, previous source intact
        assert closed["source"] and closed["runner"]  # neither leaks
        assert onboarding.read_choice(cfg) is None  # marker never persisted (prepare ran before it)
    finally:
        app._set_local_bridge(None)
        app.close()


def test_local_connect_sets_noop_reloader(tmp_path, monkeypatch):
    sock = tmp_path / "herdr.sock"
    sock.touch()
    monkeypatch.setenv("HERDR_SOCKET", str(sock))
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)

    class _Runner:
        def close(self):
            pass

    monkeypatch.setattr(srv, "_start_local_bridge", lambda sp: ("CFG", "SRV", _Runner()))
    monkeypatch.setattr(srv, "build_live_source_for_connect", lambda c, s: _DisconnectedSource())
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        _, body = _post(app, "/setup/connect", {"choice": "local"})
        assert body["ok"] is True
        local_source = app._source
        app.reload()  # a normal reloader would swap the bridge source out for mock
        assert app._source is local_source  # local mode installs a no-op reloader
    finally:
        app._set_local_bridge(None)
        app.close()


def test_demo_reload_respects_marker_over_remote_config(tmp_path, monkeypatch):
    # In demo mode a config-watch reload must NOT swap to a resolvable remote config:
    # _select_source goes through the (marker-aware) precedence, not bare select_live().
    from herdeck.deckapp import onboarding

    cfg = tmp_path / "config.toml"
    cfg.write_text('[[servers]]\nid = "herdr"\nurl = "ws://x:8788"\ntoken_env = "HERDECK_HERDR_TOKEN"\n')
    monkeypatch.setenv("HERDECK_CONFIG", str(cfg))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)
    fake = _FakeKeyring()
    fake.store[("herdeck", "HERDECK_HERDR_TOKEN")] = "tok"  # remote WOULD be usable
    monkeypatch.setattr("herdeck.secrets._keyring", lambda: fake)
    onboarding.write_choice(str(cfg), "demo")  # explicit demo choice
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    app._reloader = srv._reloader_for(app, ("mock",), srv._select_source)  # as a demo connect installs
    try:
        app.reload()  # a config-watch reload
        assert app._source.source_name == "mock"  # demo marker honored, NOT swapped to live remote
    finally:
        app.close()


def test_connect_local_bridge_start_failure(tmp_path, monkeypatch):
    sock = tmp_path / "herdr.sock"
    sock.touch()
    monkeypatch.setenv("HERDR_SOCKET", str(sock))
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("HERDECK_MOCK", raising=False)

    def _boom(socket_path):  # bridge fails to bind
        raise RuntimeError("bridge bind failed")

    monkeypatch.setattr(srv, "_start_local_bridge", _boom)
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    prev = app._source
    try:
        _, body = _post(app, "/setup/connect", {"choice": "local"})
        assert body["ok"] is False  # the bridge-start failure is caught, not propagated
        assert app._source is prev and app._local_bridge is None
    finally:
        app.close()


def test_connect_bad_body_is_400(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDECK_CONFIG", str(tmp_path / "config.toml"))
    app = srv.create_mock_app(serve=True, config_service=srv._default_config_service())
    try:
        try:
            _post(app, "/setup/connect", {"choice": "remote", "url": "", "token": ""})
            assert False, "expected 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        app.close()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_deckapp_setup_routes.py -v -k connect`
Expected: FAIL (404 for `/setup/connect`).

- [ ] **Step 3a: Add `resolve_selected_server` to ConfigService**

Add to `src/herdeck/deckapp/config_service.py` (a method on `ConfigService`, after `validate`):

```python
    def resolve_selected_server(self, data: dict, *, assume_present: str | None = None):
        """The resolved `(config, servers[0])` for the active profile, or None if there is
        no server / it cannot resolve. Only `assume_present` (the token_env about to be
        stored) is placeholdered so a not-yet-stored secret doesn't block resolution; every
        OTHER selected server's token_env must resolve for REAL. That way a config which
        would NOT load on restart (another selected server's token missing) is rejected
        here, never persisted. A caller that needs to CONNECT replaces the chosen server's
        token with the real one (`dataclasses.replace`) before building the live source."""
        from ..settings import resolve_profile

        saved: dict[str, str | None] = {}
        try:
            if assume_present and not os.environ.get(assume_present):
                saved[assume_present] = os.environ.get(assume_present)
                os.environ[assume_present] = "x"
            config = resolve_profile(self._snapshot_for(data)).config
            return (config, config.servers[0]) if config.servers else None
        except (ConfigError, KeyError, TypeError, ValueError, AttributeError):
            return None  # a parseable-but-malformed config (missing/odd keys) → not a 500
        finally:
            for n, original in saved.items():
                if original is None:
                    os.environ.pop(n, None)
                else:
                    os.environ[n] = original
```

- [ ] **Step 3b: Add `peek_keychain`, then the connect helpers and `connect`**

First add to `src/herdeck/secrets.py` (after `clear_secret`):

```python
def peek_keychain(name: str) -> str | None:
    """The keychain value for `name` (keychain only, ignoring env — unlike get_secret,
    which is env-first), or None if ABSENT. Unlike the other readers this does NOT swallow
    backend errors — it RAISES — so a caller can distinguish 'missing' (None) from
    'unreadable' (exception) and never erase a token it failed to snapshot."""
    return _keyring().get_password(SERVICE, name)
```

Add `resync` to `src/herdeck/deckapp/watcher.py` (a method on `ConfigWatcher`, after `close`):

```python
    def resync(self) -> None:
        """Adopt the current file mtimes as the baseline, so the next poll does not treat
        the latest (intentional) write as a change. Used by an in-process writer (the
        onboarding commit) that has already applied + swapped the change itself."""
        self._last = self._snapshot()
```

Make `DeckApp.reload` suppressible. In `DeckApp.__init__` (server.py), alongside `self._local_bridge = None` (Task 6), add:

```python
        self._suppress_reload = False  # set by the onboarding commit to mute the watcher
        self._setup_lock = threading.RLock()  # shared mutation lock (/setup/connect + config-write routes + reload); RLock because the config routes call reload() while holding it
```

(`threading` is already imported at the top of server.py.)

Guard `DeckApp.reload` (server.py:207) AND make it take `_setup_lock`, so a watcher-driven
reload can't run concurrently with a connect/config-write (which hold the lock for their
whole transaction — including the placeholder-env resolve). Replace the body so it reads:

```python
    def reload(self) -> None:
        """..."""  # keep the existing docstring
        if getattr(self, "_suppress_reload", False):
            return
        with self._setup_lock:  # serialize against in-flight connect / config-write transactions
            if self._reloader is not None:
                self._reloader()
```

Refactor the swap so the **render (the only fallible part) happens in prepare** and the
**commit only ASSIGNS the pre-rendered tiles** (pure dict/int ops — cannot raise). First
split the existing `_refresh_locked` (server.py:121-153) into a fallible render + a
non-failing apply, leaving its behavior identical:

```python
    def _render_locked(self, source, orch, slots):
        """Render `source` through `orch` → (tiles, panel_png, sections). This is the
        FALLIBLE part of a refresh (apply_to / orchestrator render / icon raster / panel
        compose); it mutates no self state, so it can run on a throwaway orchestrator in
        `_prepare_swap` or on the live deck inside `_refresh_locked`."""
        import io

        from ..icons import compose_panel

        source.apply_to(orch)
        rs = orch.render()
        tiles = {t.index: self._icons.render_tile_bytes(t) for t in rs.tiles if t.index < slots}
        buf = io.BytesIO()
        compose_panel(rs.panel).convert("RGB").save(buf, "PNG")
        sections = {t.index: t.section for t in rs.tiles if t.index < slots and t.section}
        return tiles, buf.getvalue(), sections

    def _apply_rendered_locked(self, tiles, panel_png, sections):
        """Assign pre-rendered tiles/panel/sections with version bumps — pure dict/int ops
        (no rendering), so it CANNOT raise. Callers hold self._lock. Byte-for-byte the tail
        of the original `_refresh_locked`."""
        for i, png in tiles.items():
            if self._tiles.get(i) != png:
                self._tile_ver[i] = self._bump()
        removed = set(self._tile_ver) - set(tiles)
        for i in removed:
            del self._tile_ver[i]
        if removed:
            self._bump()
        self._tiles = tiles
        self._tile_sections = sections
        if self._panel != panel_png:
            self._panel = panel_png
            self._panel_ver = self._bump()

    def _refresh_locked(self) -> None:
        tiles, panel_png, sections = self._render_locked(self._source, self._orch, self._slots)
        self._apply_rendered_locked(tiles, panel_png, sections)
```

Then add the swap prepare/commit (replacing the body of `swap_source`, server.py:190-205):

```python
    def _prepare_swap(self, new_source, *, clock=None):
        """Build the orchestrator AND render `new_source` into it — all the FALLIBLE parts
        of a swap (grid parse, Orchestrator construction, render). Returns a prepared bundle
        `(slots, orch, clock, tiles, panel_png, sections)` for an assignment-only commit;
        mutates NO live deck state (throwaway orchestrator), so any failure raises here,
        BEFORE anything is swapped or persisted. Pass `clock=time.monotonic` for a LIVE
        source so its elapsed-time text advances (else a connect from the mock app keeps
        the mock's frozen clock)."""
        clk = clock if clock is not None else self._clock
        cols, rows = new_source.config.grid
        slots = cols * rows - 2
        orch = Orchestrator(new_source.config, slots=slots, clock=clk)
        tiles, panel_png, sections = self._render_locked(new_source, orch, slots)
        return slots, orch, clk, tiles, panel_png, sections

    def _commit_swap(self, new_source, prepared) -> None:
        """Assign the prepared source/orchestrator/clock + its pre-rendered tiles under the
        lock — **pure assignment, no render**, so it cannot raise for a validated config:
        the post-persist swap is guaranteed not to half-swap. The single lock serializes
        against in-flight reads/presses."""
        slots, orch, clk, tiles, panel_png, sections = prepared
        with self._lock:
            old = self._source
            self._source = new_source
            self._slots = slots
            self._orch = orch
            self._clock = clk  # adopt the clock the orchestrator was built with
            new_source.attach(orch, lock=self._lock, refresh_locked=self._refresh_locked)
            self._apply_rendered_locked(tiles, panel_png, sections)
        try:
            old.close()
        except Exception:
            pass

    def swap_source(self, new_source) -> None:
        """Prepare + commit in one call (the reloader and other callers). The render runs in
        prepare (before any assignment), so a malformed config raises without half-swapping."""
        self._commit_swap(new_source, self._prepare_swap(new_source))
```

Then add to server.py (top-level, near `select_source_kind`). Note `_probe_sync` and `build_live_source_for_connect` are module-level so tests can monkeypatch them:

```python
def _token_env_for(server_id: str) -> str:
    slug = "".join(c if c.isalnum() else "_" for c in server_id).upper()
    return f"HERDECK_{slug}_TOKEN"


def _restore_secret(name: str, prior: str | None) -> None:
    """Restore the keychain entry for `name` to its snapshot `prior`: re-store the prior
    value if it existed, else clear it. So a rollback after overwriting an existing token
    (reconnecting an existing server) never destroys the previously-stored secret.
    Best-effort: never raises."""
    from .. import secrets as secret_store

    try:
        if prior is None:
            secret_store.clear_secret(name)
        else:
            secret_store.set_secret(name, prior)
    except Exception:
        pass


def _restore_file(path, prior_text) -> None:
    """Restore a file to its prior contents (or remove it if it did not exist before).
    Used to undo a partial/failed config write so no serverful-but-tokenless config is
    left behind. Best-effort: never raises."""
    try:
        if prior_text is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(prior_text, encoding="utf-8")
    except OSError:
        pass


def _snapshot_config(svc):
    """Read the current config.toml/local.toml text (or None if absent) for rollback.
    Raises OSError on a read fault — the caller snapshots BEFORE mutating anything, so a
    failure here persists nothing."""
    cfg = svc._config_path.read_text(encoding="utf-8") if svc._config_path.exists() else None
    local = svc._local_path.read_text(encoding="utf-8") if svc._local_path.exists() else None
    return cfg, local


def _restore_choice(config_path, prior: str | None) -> None:
    """Restore the onboarding marker to its snapshot: re-write the prior choice if there
    was one, else clear it. Used to undo a local connect whose swap failed after the
    marker was written. Best-effort: never raises."""
    from .onboarding import clear_choice, write_choice

    try:
        if prior is None:
            clear_choice(config_path)
        else:
            write_choice(config_path, prior)
    except OSError:
        pass


def _probe_sync(url: str, token: str):
    """Sync wrapper over the async probe (the HTTP handler runs on a plain thread)."""
    import asyncio

    from .probe import probe_server

    return asyncio.run(probe_server(url, token))


def build_live_source_for_connect(config, server):
    from .live import build_live_source

    return build_live_source(config, server)


def connect(app, body) -> dict | None:
    """Run the onboarding connect flow. Returns the response dict, or None for a
    malformed body (the route maps None -> HTTP 400). Live swaps follow
    build -> swap -> adopt so a failed build never strands the app on a closed
    bridge; remote builds the live source BEFORE persisting (no half-commit)."""
    import dataclasses
    import time
    import tomllib

    from .mock import MockSource
    from .onboarding import read_choice, write_choice

    choice = body.get("choice")
    config_path = str(app._config_service._config_path) if app._config_service else None

    if choice == "demo":
        # Transactional like local/remote: prepare (render mock) BEFORE the marker, commit after.
        prior_choice = read_choice(config_path)
        new_source = MockSource()
        try:
            prepared = app._prepare_swap(new_source)  # render mock (fallible) BEFORE persisting
            write_choice(config_path, "demo")  # persist
        except Exception:
            _restore_choice(config_path, prior_choice)
            new_source.close()
            return {"ok": False, "error": "could not switch to demo"}
        app._commit_swap(new_source, prepared)  # assignment-only
        app._set_local_bridge(None)
        app._reloader = _reloader_for(app, ("mock",), _select_source)  # mock/remote reloads resume
        return {"ok": True}

    if choice == "local":
        from ..bootstrap import resolve_socket_path

        socket_path = resolve_socket_path(None)
        if not os.path.exists(socket_path):
            return {"ok": False, "error": f"herdr socket not found at {socket_path}"}
        new_source = None
        runner = None
        prior_choice = read_choice(config_path)  # snapshot the marker for rollback
        try:
            config, server, runner = _start_local_bridge(socket_path)  # may raise (bridge bind)
            new_source = build_live_source_for_connect(config, server)  # build ...
            prepared = app._prepare_swap(new_source, clock=time.monotonic)  # ... pre-build orch (live clock) BEFORE the marker ...
            write_choice(config_path, "local")  # ... persist (durable)
        except Exception:
            _restore_choice(config_path, prior_choice)  # undo the marker if it was written
            if new_source is not None:
                new_source.close()  # don't leak the built source / its connector runner
            if runner is not None:
                runner.close()  # ... or the just-started bridge; previous source untouched
            return {"ok": False, "error": "could not start local source"}
        app._commit_swap(new_source, prepared)  # non-failing: all fallible work done; sets the live clock
        app._set_local_bridge(runner)  # adopt new bridge (closes old one)
        app._reloader = _reloader_for(app, ("local",), _select_source)  # no-op: don't swap out the bridge
        return {"ok": True, "connected": app._source.connected}

    if choice == "remote":
        url, token, server_id = body.get("url"), body.get("token"), body.get("id") or "herdr"
        if not (isinstance(url, str) and url and isinstance(token, str) and token
                and isinstance(server_id, str) and server_id):
            return None  # -> 400: url/token/id must be non-empty strings (e.g. {"id": 123} is invalid)
        token_env = _token_env_for(server_id)
        # Secret resolution is ENV-FIRST: if token_env is already exported with a DIFFERENT
        # value, that env value would shadow whatever we store in the keychain, so the
        # persisted config would NOT resolve to the typed token. Reject before doing anything.
        env_token = os.environ.get(token_env)
        if env_token is not None and env_token != token:
            return {
                "ok": False,
                "error": f"{token_env} is set in the environment and would override the saved token; unset it or connect with that value",
            }
        result = _probe_sync(url, token)
        if not result.ok:
            return {"ok": False, "error": result.reason}
        try:
            data = app._config_service.read()  # a malformed/unreadable existing config must not 500
        except (OSError, tomllib.TOMLDecodeError):
            return {"ok": False, "error": "existing config is unreadable — fix it in Settings"}
        payload = {
            "base": dict(data.get("base") or {}),
            "profiles": data.get("profiles") or {},
            "local": data.get("local") or {},
        }
        existing = payload["base"].get("servers")
        if existing is not None and not (isinstance(existing, list) and all(isinstance(s, dict) for s in existing)):
            # parseable TOML but a wrong shape (e.g. `servers = ["bad"]`) would crash the upsert
            return {"ok": False, "error": "existing config is malformed (servers) — fix it in Settings"}
        servers = list(existing or [])
        entry = {"id": server_id, "url": url, "token_env": token_env}
        for i, s in enumerate(servers):
            if s.get("id") == server_id:
                servers[i] = entry
                break
        else:
            servers.append(entry)
        payload["base"]["servers"] = servers
        # token_env (HERDECK_<ID>_TOKEN) lives in ONE flat keychain namespace shared by ALL
        # config sections — other servers, `notifications.telegram`, profile overlays. Two ids
        # can collide (`foo-bar`/`foo_bar`), and a derived name can clash with a NON-server
        # secret. Collect every token_env the EXISTING config references except the server we
        # are replacing; reject if ours is already in use, so we never overwrite another secret.
        from .config_service import ConfigService

        base_wo_ours = dict(data.get("base") or {})
        base_wo_ours["servers"] = [
            s for s in (base_wo_ours.get("servers") or [])
            if not (isinstance(s, dict) and s.get("id") == server_id)
        ]
        in_use = []
        ConfigService._collect_token_envs(base_wo_ours, in_use)
        ConfigService._collect_token_envs(data.get("profiles") or {}, in_use)
        if token_env in in_use:
            return {
                "ok": False,
                "error": f"token env {token_env} is already used elsewhere in the config — pick a different id",
            }
        # BUILD-BEFORE-PERSIST: resolve the merged payload (placeholder tokens) to confirm
        # selection, then build the live source with the REAL token baked into the chosen
        # ServerConfig — all BEFORE mutating keychain/config, so any selection / validation
        # / build failure persists NOTHING (no orphaned secret, no serverful-but-dead config).
        resolved = app._config_service.resolve_selected_server(payload, assume_present=token_env)
        if resolved is None or resolved[1].id != server_id:
            return {
                "ok": False,
                "error": "config does not resolve to this server (check the active profile / overview_order / other servers' tokens) — fix it in Settings",
            }
        config, placeholder_server = resolved
        real_server = dataclasses.replace(placeholder_server, token=token)  # real token, not keychain
        try:
            new_source = build_live_source_for_connect(config, real_server)  # build BEFORE persist
        except Exception:
            return {"ok": False, "error": "could not build the remote source"}
        # Persist + swap as one watcher-suppressed transaction (see _commit_remote).
        return _commit_remote(app, payload, token_env, token, new_source, config_path)

    return None  # unknown choice -> 400


def _commit_remote(app, payload, token_env, token, new_source, config_path) -> dict:
    """Persist (secret-then-config) and swap to `new_source` as ONE transaction, with the
    config watcher SUPPRESSED so its mtime poll can't reload mid-commit (double-swapping
    to a second source) or swap to the half-written config during a rollback. Any failure
    restores the prior secret + config and closes the just-built source. The watcher
    baseline is resynced on exit so it doesn't fire on our own writes/restores."""
    import time

    from .. import secrets as secret_store
    from .onboarding import clear_choice

    app._suppress_reload = True
    try:
        # Pre-build the orchestrator (the only fallible part of the swap) BEFORE persisting,
        # so the post-persist commit (_commit_swap) is guaranteed non-throwing.
        try:
            prepared = app._prepare_swap(new_source, clock=time.monotonic)  # live clock
        except Exception:
            new_source.close()
            return {"ok": False, "error": "could not build the remote source"}
        # Snapshot the prior keychain value AND the on-disk config BEFORE any mutation, so a
        # read fault can't strand a secret, and a partial write (config ok, local faults) is
        # undone — never leaving a serverful-but-tokenless config or a destroyed prior token.
        # peek_keychain raises (not None) on a backend READ error, so we abort here rather
        # than risk erasing an existing token we couldn't actually read.
        try:
            prior_secret = secret_store.peek_keychain(token_env)
        except Exception:
            new_source.close()
            return {"ok": False, "error": "could not read the existing token — check the keychain"}
        svc = app._config_service
        try:
            prior_config, prior_local = _snapshot_config(svc)
        except OSError:
            new_source.close()  # nothing mutated yet
            return {"ok": False, "error": "could not read config"}
        try:
            secret_store.set_secret(token_env, token)
        except Exception:
            _restore_secret(token_env, prior_secret)  # set may have partially overwritten
            new_source.close()
            return {"ok": False, "error": "could not store token"}

        def _rollback():
            _restore_file(svc._config_path, prior_config)
            _restore_file(svc._local_path, prior_local)
            _restore_secret(token_env, prior_secret)  # restore prior token, don't destroy it
            new_source.close()

        try:
            errors = svc.write(payload)
        except OSError:  # atomic write can fault, possibly after a partial write
            _rollback()
            return {"ok": False, "error": "could not write config"}
        if errors:  # structural validation runs before any write, so nothing was written
            _rollback()
            return {"ok": False, "error": "; ".join(errors)}
        # Clear the stale local/demo marker as PART OF THE COMMIT: remote == a usable config,
        # no opt-in marker. If the unlink faults, roll everything back so a later-removed
        # config falls to first_run (the card), never to a stale marker that would mask it.
        try:
            clear_choice(config_path)
        except OSError:
            _rollback()
            return {"ok": False, "error": "could not finalize onboarding"}
        app._commit_swap(new_source, prepared)  # non-failing: all fallible work done; sets the live clock
        app._set_local_bridge(None)  # ... then drop any local bridge
        app._reloader = _reloader_for(app, ("remote",), _select_source)  # config-edit reloads resume
        return {"ok": True, "connected": app._source.connected}  # honest: connector dials async
    finally:
        watcher = getattr(app, "_watcher", None)
        if watcher is not None:
            watcher.resync()  # adopt our writes as the baseline; no spurious reload
        app._suppress_reload = False
```

- [ ] **Step 4: Add the route**

In `do_POST` (server.py), extend the route set. Change the condition guarding the config routes to also accept `/setup/connect`, and add the branch. Insert this branch before the existing `elif path in ("/config/validate", ...)` chain check — simplest is a dedicated branch:

```python
                elif path == "/setup/connect":
                    if not self._require_header_token():
                        return
                    body = self._json_body()
                    if body is _BAD_BODY:
                        return
                    with app._setup_lock:  # serialize concurrent connects (ThreadingHTTPServer)
                        result = connect(app, body)
                    if result is None:
                        self._send(400)
                        return
                    self._send(200, json.dumps(result).encode(), "application/json")
```

`_setup_lock` is the **shared durable-state mutation lock**, NOT just for connects: the
same `ThreadingHTTPServer` also serves the config-editor write routes concurrently, and a
concurrent edit could interleave with a connect's snapshot/write/rollback. So **also hold
`app._setup_lock` across the mutation in each existing config-mutation route** — the
`do_POST` branches for `/config`, `/profiles/active`, `/secret`, and the `do_DELETE`
`/secret/{env}` branch. Wrap the body of each (the `_config_service.write` /
`set_active` / `set_secret` / `clear_secret` call + any `app.reload()`), e.g.:

```python
                    elif path == "/config":
                        body = self._json_body()
                        if body is _BAD_BODY:
                            return
                        with app._setup_lock:
                            errors = app._config_service.write(body)
                            if not errors:
                                app.reload()
                        self._send(200, json.dumps({"errors": errors}).encode(), "application/json")
```

(`/config/validate` is read-only — it does NOT take the lock.) **The config routes do NOT
touch the onboarding marker** — the marker is the user's explicit *connection* choice and
is changed ONLY by `/setup/connect` (remote clears it, local/demo set it). So a user in
local/demo mode who edits the config (even adding a server) STAYS in that mode until they
explicitly re-onboard to remote; editing config content never silently switches the
connection mode. (This is the deliberate resolution of the marker-vs-remote-config
precedence: an explicit choice sticks; the editor edits content, not mode.)

- [ ] **Step 5: Run to verify it passes (whole setup-routes file)**

Run: `.venv/bin/python -m pytest tests/test_deckapp_setup_routes.py -v`
Expected: PASS (status tests from Task 7 + 4 connect tests).

- [ ] **Step 6: Lint + commit**

```bash
.venv/bin/ruff check src tests
git add src/herdeck/secrets.py src/herdeck/deckapp/config_service.py src/herdeck/deckapp/server.py tests/test_deckapp_setup_routes.py
git commit -m "feat(deckapp): POST /setup/connect (local/remote/demo, build-before-persist)"
```

---

### Task 9: Rust proxy commands

Two token-injecting Tauri commands so the WebView reaches `/setup*` without seeing the access token, with a dedicated longer timeout for connect.

**Files:**
- Modify: `desktop/src-tauri/src/http.rs` (proxy helpers + tests)
- Modify: `desktop/src-tauri/src/lib.rs` (commands + timeout + registration)
- Test: `desktop/src-tauri/tests/http.rs`

**Interfaces:**
- Consumes: `http::http_get`, `http::http_post_json`.
- Produces:
  - `http::fetch_setup(host, port, token, timeout) -> Result<String, String>` (GET `/setup?token=…`).
  - `http::post_setup_connect(host, port, token, body, timeout) -> Result<(u16, String), String>` (POST `/setup/connect`, `X-Herdeck-Token`).
  - Commands `setup_status` / `setup_connect`; const `SETUP_CONNECT_TIMEOUT = Duration::from_secs(15)` (covers the full remote transaction, not just the probe).

- [ ] **Step 1: Write the failing tests**

Append to `desktop/src-tauri/tests/http.rs`:

```rust
use herdeck_desktop_lib::http::{fetch_setup, post_setup_connect};

#[test]
fn fetch_setup_injects_token_as_query_param() {
    let (port, rx) = serve_once_capture(
        b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n{\"mode\":\"mock\",\"reason\":\"first_run\"}".to_vec(),
    );
    let body = fetch_setup("127.0.0.1", port, "SECRET", Duration::from_secs(2)).unwrap();
    assert!(body.contains("\"reason\":\"first_run\""));
    let req = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert!(req.starts_with("GET /setup?token=SECRET HTTP/1.0"), "request was: {req:?}");
}

#[test]
fn post_setup_connect_sends_header_token_and_body() {
    let (port, rx) = serve_once_capture(
        b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n{\"ok\":true,\"connected\":true}".to_vec(),
    );
    let (code, body) = post_setup_connect(
        "127.0.0.1", port, "HDR", "{\"choice\":\"demo\"}", Duration::from_secs(2),
    )
    .unwrap();
    assert_eq!(code, 200);
    assert!(body.contains("\"ok\":true"));
    let req = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert!(req.starts_with("POST /setup/connect HTTP/1.0"), "request was: {req:?}");
    assert!(req.contains("X-Herdeck-Token: HDR\r\n"), "request was: {req:?}");
    assert!(req.ends_with("{\"choice\":\"demo\"}"), "request was: {req:?}");
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd desktop/src-tauri && ~/.cargo/bin/cargo test --test http`
Expected: FAIL to compile (`fetch_setup`/`post_setup_connect` unresolved).

- [ ] **Step 3: Add the proxy helpers to http.rs**

Add to `desktop/src-tauri/src/http.rs` in the proxy layer section (after `send_press`):

```rust
/// Proxy `GET /setup`, injecting the token as a query param. Returns the JSON body.
pub fn fetch_setup(host: &str, port: u16, token: &str, timeout: Duration) -> Result<String, String> {
    http_get(host, port, &format!("/setup?token={token}"), timeout)
}

/// Proxy `POST /setup/connect` with the token in the `X-Herdeck-Token` header and a
/// JSON body. Returns `(status, body)` for all statuses (200 carries `{ok,…}`, 400 a
/// malformed body), matching `http_post_json`.
pub fn post_setup_connect(
    host: &str,
    port: u16,
    token: &str,
    body: &str,
    timeout: Duration,
) -> Result<(u16, String), String> {
    http_post_json(host, port, "/setup/connect", ("X-Herdeck-Token", token), body, timeout)
}
```

- [ ] **Step 4: Add the commands to lib.rs**

In `desktop/src-tauri/src/lib.rs`, after `SIDECAR_TIMEOUT` (line 34) add:

```rust
/// `/setup/connect` runs, inside the sidecar, the whole remote transaction: a probe
/// (≈4 s) THEN build + render-prepare + keychain/config snapshots + write + swap. The
/// proxy must comfortably outlast the full worst case (not just the probe) so it never
/// times out while the sidecar is mid-persist (a torn result). 15 s leaves wide margin
/// over the 4 s probe + the sub-second post-probe work; far above the 3 s SIDECAR_TIMEOUT.
const SETUP_CONNECT_TIMEOUT: Duration = Duration::from_secs(15);
```

Add the two commands (near the config commands):

```rust
/// Proxy `GET /setup` (token as query param) → the first-run status JSON.
#[tauri::command]
fn setup_status(state: tauri::State<'_, AppState>) -> Result<serde_json::Value, String> {
    let d = current_discovery(&state)?;
    let body = http::fetch_setup(&d.host, d.port, &d.token, SIDECAR_TIMEOUT)?;
    serde_json::from_str(&body).map_err(|e| format!("invalid /setup JSON from sidecar: {e}"))
}

/// Proxy `POST /setup/connect` (header token) → the connect result `{ok, …}`. Uses a
/// dedicated timeout longer than the sidecar's remote probe. The typed token VALUE is
/// in the forwarded body; it is never read back.
#[tauri::command]
fn setup_connect(
    state: tauri::State<'_, AppState>,
    body: serde_json::Value,
) -> Result<serde_json::Value, String> {
    let d = current_discovery(&state)?;
    let (code, resp) = http::post_setup_connect(
        &d.host, d.port, &d.token, &body.to_string(), SETUP_CONNECT_TIMEOUT,
    )?;
    if code == 200 {
        serde_json::from_str(&resp).map_err(|e| format!("invalid /setup/connect JSON: {e}"))
    } else {
        Err(format!("sidecar returned HTTP {code} for /setup/connect"))
    }
}
```

Register both in `invoke_handler!` (lib.rs:421-435), adding after `config_secret_clear`:

```rust
            setup_status,
            setup_connect,
```

- [ ] **Step 5: Run the Rust suite**

Run: `cd desktop/src-tauri && ~/.cargo/bin/cargo test`
Expected: PASS (new http tests + existing suite; build clean).

- [ ] **Step 6: Commit**

```bash
git add desktop/src-tauri/src/http.rs desktop/src-tauri/src/lib.rs desktop/src-tauri/tests/http.rs
git commit -m "feat(desktop): setup_status/setup_connect token-injecting proxies"
```

---

### Task 10: Frozen sidecar wiring

Ensure the local-bridge path is bundled and reachable in the frozen sidecar, and prove it in the smoke gate.

**Files:**
- Modify: `desktop/herdeck-deckapp.spec`
- Modify: `desktop/scripts/smoke-sidecar.sh`

**Interfaces:**
- Consumes: the new `herdeck.deckapp.{onboarding,local_bridge,probe}` modules + `herdeck.bridge`/`herdeck.bootstrap`.

- [ ] **Step 1: Add the local-path hiddenimports to the spec**

In `desktop/herdeck-deckapp.spec`, extend `hiddenimports` (lines 21-30) to include the local-bridge graph (belt-and-suspenders over PyInstaller's analysis):

```python
    hiddenimports=[
        "herdeck.deckapp.server",
        "herdeck.deckapp.live",
        "herdeck.deckapp.mock",
        "herdeck.deckapp.source",
        "herdeck.deckapp.watcher",
        "herdeck.deckapp.config_service",
        "herdeck.deckapp.onboarding",
        "herdeck.deckapp.local_bridge",
        "herdeck.deckapp.probe",
        "herdeck.bridge",
        "herdeck.bootstrap",
        "herdeck.connector",
        "websockets",
        "tomli_w",
    ],
```

- [ ] **Step 2: Add an import-reachability check to the smoke script**

In `desktop/scripts/smoke-sidecar.sh`, after the baked-glyph block and before `export HERDECK_MOCK=1` (line 52), add a frozen import probe. The frozen binary, run with `HERDECK_SELFTEST=imports`, must be taught to import the local path and exit 0 (added in Step 3):

```bash
# --- Frozen local-bridge import reachability ---------------------------------------
# The local onboarding path pulls herdeck.bridge/bootstrap + the new deckapp modules.
# A full local connect needs a herdr socket (unit-tested via StubHerdr), but assert
# here that the FROZEN binary can import the whole local path (no missing hiddenimport).
# Self-contained temp file (defined + cleaned up here) so it is safe under `set -u`,
# regardless of where it sits relative to the other mktemp lines.
IMPORT_ERR="$(mktemp)"
if ! HERDECK_SELFTEST=imports "$BIN" >/dev/null 2>"$IMPORT_ERR"; then
  echo "FAIL: frozen local-bridge imports unreachable"; cat "$IMPORT_ERR"; rm -f "$IMPORT_ERR"; exit 1
fi
rm -f "$IMPORT_ERR"
echo "OK: frozen local-bridge imports reachable"
```

- [ ] **Step 3: Teach the entrypoint a self-test mode**

In `src/herdeck/deckapp/__main__.py`, at the very top of `main()` (after the docstring, before reading `HERDECK_DECKAPP_PORT`), add a self-test shortcut that imports the local path and exits without starting a server:

```python
    if os.environ.get("HERDECK_SELFTEST") == "imports":
        import importlib

        for mod in (
            "herdeck.deckapp.onboarding",
            "herdeck.deckapp.local_bridge",
            "herdeck.deckapp.probe",
            "herdeck.bridge",
            "herdeck.bootstrap",
            "herdeck.connector",
        ):
            importlib.import_module(mod)
        return 0
```

- [ ] **Step 4: Run the unit test for the self-test path (non-frozen)**

Add to `tests/test_deckapp_entry.py` (or create `tests/test_deckapp_selftest.py`):

```python
# tests/test_deckapp_selftest.py
import os
import subprocess
import sys


def test_selftest_imports_exits_zero():
    # Preserve the env and prepend the repo `src` to PYTHONPATH so `-m herdeck.deckapp`
    # resolves even in a clean checkout (package not installed editable).
    repo_src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    env = {**os.environ, "HERDECK_SELFTEST": "imports"}
    env["PYTHONPATH"] = repo_src + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run(
        [sys.executable, "-m", "herdeck.deckapp"],
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert r.returncode == 0, r.stderr.decode()
```

Run: `.venv/bin/python -m pytest tests/test_deckapp_selftest.py -v`
Expected: PASS.

- [ ] **Step 5: Run the REAL freeze + smoke gate**

Run: `bash desktop/scripts/build-sidecar.sh && bash desktop/scripts/smoke-sidecar.sh`
Expected: the build succeeds; smoke prints `OK: baked codex PNG decodes …`, `OK: frozen local-bridge imports reachable`, the three `OK: … -> 200`, and `SMOKE PASS`.

- [ ] **Step 6: Lint + commit**

```bash
.venv/bin/ruff check src tests
git add desktop/herdeck-deckapp.spec desktop/scripts/smoke-sidecar.sh src/herdeck/deckapp/__main__.py tests/test_deckapp_selftest.py
git commit -m "build(desktop): bundle + smoke the frozen local-bridge path"
```

---

## Final verification (after all tasks)

Run the full gate:

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/ruff check src tests
cd desktop/src-tauri && ~/.cargo/bin/cargo test && cd ../..
bash desktop/scripts/build-sidecar.sh && bash desktop/scripts/smoke-sidecar.sh
```

Expected: pytest green, ruff clean, cargo green, freeze+smoke `SMOKE PASS`. (No frontend in this slice — the `<Onboarding>` card + `App.svelte` wiring + `onboardingClient.ts` are 3c-ii.)

## Out of scope (this slice)

- The `<Onboarding>` card, `App.svelte` status poll, `onboardingClient.ts` — **3c-ii**.
- Reload-driven local↔remote transitions beyond the explicit `/setup/connect` path (config-editor edit in local mode does not tear down the embedded bridge — documented follow-up).
- `config.hardware.herdr_socket` override for the deckapp local path (the deckapp resolves `HERDR_SOCKET` env → XDG default; the CLI keeps the config override).
- The manual `.app` first-run gate (double-click on a Mac → card → connect → deck), verified by the user.
