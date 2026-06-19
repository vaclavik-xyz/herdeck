# Zero-config Local Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `herdeck` run with no config file, no token, and no separately started bridge when herdr is on the same machine — auto-detected — and fall back to the web simulator when no Stream Deck is attached.

**Architecture:** In local mode the app starts an embedded loopback bridge (`bridge.start_local_bridge`) on an ephemeral port with a random in-memory token, then drives the existing WebSocket `Connector` against it — so the local and remote data paths are identical. Config (`[[servers]]`, `answer_profiles`) becomes optional; built-in defaults fill the gaps. A pure `resolve_mode` function picks mock/remote/local/error.

**Tech Stack:** Python ≥3.12, `asyncio`, `websockets`, `tomllib`, `pytest` + `pytest-asyncio` (asyncio_mode=auto, already configured).

## Global Constraints

- Python ≥ 3.12; only runtime dependency is `websockets>=14`.
- Loopback bind only (`127.0.0.1`); never `0.0.0.0`. Token is random, in-memory, never written to disk.
- Remote mode (config with `[[servers]]`) must behave exactly as today — no regressions.
- Preserve the existing ordering rule: load config BEFORE constructing the deck (the D200 driver `os.chdir`s in `__init__`).
- TDD: failing test first, minimal code, commit per task. Conventional commit messages.
- Every commit triggers an automatic roborev review (post-commit hook). After a task's commit, if roborev reports a finding, fix it (TDD) before moving on.

---

### Task 1: Shared `DEFAULT_PROFILES` constant

**Files:**
- Modify: `src/herdeck/config.py`
- Modify: `src/herdeck/app.py` (`_mock_config`)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `herdeck.config.DEFAULT_PROFILES: dict[str, AnswerProfile]` with keys `claude`, `codex`, `default`.

- [ ] **Step 1: Write the failing test**

In `tests/test_config.py`, add:

```python
from herdeck.config import DEFAULT_PROFILES, AnswerProfile


def test_default_profiles_cover_claude_codex_default():
    assert set(DEFAULT_PROFILES) == {"claude", "codex", "default"}
    assert isinstance(DEFAULT_PROFILES["default"], AnswerProfile)
    assert DEFAULT_PROFILES["claude"].approve == ["1", "enter"]
    assert DEFAULT_PROFILES["claude"].approve_always == ["2", "enter"]
    assert DEFAULT_PROFILES["default"].stop == ["ctrl+c"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_config.py::test_default_profiles_cover_claude_codex_default -v`
Expected: FAIL with `ImportError: cannot import name 'DEFAULT_PROFILES'`.

- [ ] **Step 3: Add the constant**

In `src/herdeck/config.py`, after `DEFAULT_START_PROFILES`:

```python
# Built-in answer profiles used when a config omits them (and by local mode).
DEFAULT_PROFILES: dict[str, AnswerProfile] = {
    "claude": AnswerProfile(["1", "enter"], ["esc"], ["ctrl+c"], ["2", "enter"]),
    "codex": AnswerProfile(["y", "enter"], ["n", "enter"], ["ctrl+c"], ["y", "enter"]),
    "default": AnswerProfile(["enter"], ["esc"], ["ctrl+c"], ["enter"]),
}
```

- [ ] **Step 4: Reuse it in `_mock_config`**

In `src/herdeck/app.py`, change `_mock_config` to import and use `DEFAULT_PROFILES`:

```python
def _mock_config() -> Config:
    """A zero-setup config for the offline simulator (no file/token needed)."""
    from .config import DEFAULT_PROFILES, ServerConfig
    return Config(
        servers=[ServerConfig("mock", "ws://mock", "x")],
        profiles=dict(DEFAULT_PROFILES),
        overview_order=["mock"],
        grid=(5, 3),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_config.py tests/test_app.py -q`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/config.py src/herdeck/app.py tests/test_config.py
git commit -m "refactor(config): extract DEFAULT_PROFILES; reuse in mock config"
```

---

### Task 2: Optional `[[servers]]` and `answer_profiles` in `load_config`

**Files:**
- Modify: `src/herdeck/config.py` (`load_config`)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `load_config(path)` no longer raises when `[[servers]]` is absent
  (`servers == []`) nor when `answer_profiles` is absent/partial (missing
  profiles come from `DEFAULT_PROFILES`; provided ones override). Present
  servers still validate `token_env` (unchanged).

- [ ] **Step 1: Write the failing tests**

In `tests/test_config.py`, add:

```python
SERVERLESS = """
[answer_profiles.codex]
approve = ["y", "enter"]
deny = ["n", "enter"]
stop = ["ctrl+c"]
"""


def test_config_without_servers_yields_empty_list(tmp_path):
    cfg = load_config(_write(tmp_path, SERVERLESS))
    assert cfg.servers == []


def test_missing_answer_profiles_fall_back_to_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, "[deck]\ngrid = \"5x3\"\n"))
    assert cfg.profiles["default"].approve == ["enter"]
    assert cfg.profiles["claude"].approve == ["1", "enter"]


def test_partial_answer_profiles_merge_over_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, SERVERLESS))
    assert cfg.profiles["codex"].approve == ["y", "enter"]   # overridden
    assert cfg.profiles["default"].approve == ["enter"]      # from defaults
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_config.py -k "servers or defaults or merge" -v`
Expected: FAIL — `load_config` currently raises `ConfigError("no [[servers]] configured")` / `ConfigError("answer_profiles.default is required")`.

- [ ] **Step 3: Relax `load_config`**

In `src/herdeck/config.py`, in `load_config`, remove the `if not servers: raise`
block, and replace the profiles block. The relevant parts become:

```python
    servers = []
    for s in data.get("servers", []):
        env = s["token_env"]
        token = os.environ.get(env)
        if not token:
            raise ConfigError(f"env var '{env}' for server '{s['id']}' is not set")
        servers.append(ServerConfig(id=s["id"], url=s["url"], token=token))
    # NOTE: empty servers is allowed; the remote run path requires >=1 itself.

    deck = data.get("deck", {})
    grid = _parse_grid(deck.get("grid", "5x3"))
    overview_order = deck.get("overview_order", [s.id for s in servers])

    profiles = dict(DEFAULT_PROFILES)            # built-ins; config overrides below
    for name, raw in data.get("answer_profiles", {}).items():
        profiles[name] = _parse_profile(name, raw)
```

(Delete the old `if "default" not in profiles: raise` check — defaults always
include `default`.)

- [ ] **Step 4: Run the full config suite to verify pass + no regressions**

Run: `.venv/bin/python -m pytest tests/test_config.py -q`
Expected: PASS (including the existing `test_load_resolves_token_from_env`,
`test_missing_token_env_raises`, `test_profile_approve_always_defaults_to_approve`).

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/config.py tests/test_config.py
git commit -m "feat(config): make servers and answer_profiles optional"
```

---

### Task 3: `resolve_mode` pure function

**Files:**
- Modify: `src/herdeck/app.py`
- Test: `tests/test_local_mode.py` (create)

**Interfaces:**
- Produces: `herdeck.app.resolve_mode(*, mock: bool, config_path: str | None, config_has_servers: bool, socket_path: str, socket_exists: bool) -> tuple`
  returning `("mock",)`, `("remote", config_path)`, `("local", socket_path)`, or `("error", message)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_local_mode.py`:

```python
from herdeck.app import resolve_mode

SOCK = "/Users/x/.config/herdr/herdr.sock"


def test_mock_wins():
    assert resolve_mode(mock=True, config_path="/c", config_has_servers=True,
                        socket_path=SOCK, socket_exists=True) == ("mock",)


def test_config_with_servers_is_remote():
    assert resolve_mode(mock=False, config_path="/c", config_has_servers=True,
                        socket_path=SOCK, socket_exists=True) == ("remote", "/c")


def test_socket_without_servers_is_local():
    assert resolve_mode(mock=False, config_path=None, config_has_servers=False,
                        socket_path=SOCK, socket_exists=True) == ("local", SOCK)


def test_serverless_config_plus_socket_is_local():
    assert resolve_mode(mock=False, config_path="/c", config_has_servers=False,
                        socket_path=SOCK, socket_exists=True) == ("local", SOCK)


def test_no_socket_no_servers_is_error():
    mode = resolve_mode(mock=False, config_path=None, config_has_servers=False,
                        socket_path=SOCK, socket_exists=False)
    assert mode[0] == "error" and SOCK in mode[1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_local_mode.py -v`
Expected: FAIL with `ImportError: cannot import name 'resolve_mode'`.

- [ ] **Step 3: Implement `resolve_mode`**

In `src/herdeck/app.py` (module level):

```python
def resolve_mode(*, mock, config_path, config_has_servers, socket_path,
                 socket_exists):
    """Decide how to run from already-gathered facts (pure; no IO)."""
    if mock:
        return ("mock",)
    if config_path is not None and config_has_servers:
        return ("remote", config_path)
    if socket_exists:
        return ("local", socket_path)
    return ("error",
            f"No herdr socket at {socket_path} and no [[servers]] config. "
            f"Is herdr running? Set HERDR_SOCKET or create a config "
            f"(see config.example.toml).")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_local_mode.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/app.py tests/test_local_mode.py
git commit -m "feat(app): resolve_mode (mock/remote/local/error)"
```

---

### Task 4: Deck selection with web fallback

**Files:**
- Modify: `src/herdeck/app.py`
- Test: `tests/test_local_mode.py`

**Interfaces:**
- Produces: `herdeck.app.make_deck(kind, slots, *, d200_factory=None, web_factory=None) -> DeckDriver`.
  `kind` is `"d200" | "web" | "fake" | None`. `None` = auto: try d200, fall back
  to web on failure. Explicit `"d200"` propagates the failure (no fallback).

- [ ] **Step 1: Write the failing tests**

In `tests/test_local_mode.py`, add:

```python
import pytest
from herdeck.app import make_deck
from herdeck.driver.fake import FakeRenderer


class _Web:
    def __init__(self): self.kind = "web"


def _boom():
    raise RuntimeError("no device")


def test_auto_falls_back_to_web_when_d200_unavailable():
    deck = make_deck(None, 13, d200_factory=_boom, web_factory=_Web)
    assert isinstance(deck, _Web)


def test_explicit_d200_failure_propagates():
    with pytest.raises(RuntimeError):
        make_deck("d200", 13, d200_factory=_boom, web_factory=_Web)


def test_fake_kind_returns_fake_renderer():
    deck = make_deck("fake", 13, d200_factory=_boom, web_factory=_Web)
    assert isinstance(deck, FakeRenderer)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_local_mode.py -k make or deck -v`
Expected: FAIL with `ImportError: cannot import name 'make_deck'`.

- [ ] **Step 3: Implement `make_deck`**

In `src/herdeck/app.py`:

```python
def make_deck(kind, slots, *, d200_factory=None, web_factory=None):
    """Build the deck driver. kind None => auto (d200, else web fallback)."""
    import os

    if web_factory is None:
        def web_factory():
            from .driver.web import WebDeck
            host = os.environ.get("HERDECK_WEB_BIND", "127.0.0.1")
            port = int(os.environ.get("HERDECK_WEB_PORT", "8800"))
            d = WebDeck(slots, host=host, port=port)
            print(f"herdeck web simulator on http://{d.host}:{d.port}")
            return d

    if d200_factory is None:
        def d200_factory():
            from .driver.d200 import D200Driver
            return D200Driver()

    if kind == "fake":
        return FakeRenderer(slots)
    if kind == "web":
        return web_factory()
    if kind == "d200":
        return d200_factory()                 # explicit: do not swallow failure
    try:                                       # auto
        return d200_factory()
    except Exception as exc:
        print(f"No Stream Deck opened ({exc}); close Ulanzi Studio if it is "
              f"running. Falling back to the web simulator.")
        return web_factory()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_local_mode.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/app.py tests/test_local_mode.py
git commit -m "feat(app): make_deck with web fallback when no deck attached"
```

---

### Task 5: `start_local_bridge` embedded loopback bridge

**Files:**
- Modify: `src/herdeck/bridge.py`
- Test: `tests/test_local_mode.py`

**Interfaces:**
- Consumes: `bridge.SocketHerdr`, `bridge.HerdrEvents`, `bridge._serve_connection`,
  `bridge._broadcast`, `bridge.StubHerdr` (test stub, already present).
- Produces: `async bridge.start_local_bridge(socket_path, host="127.0.0.1", herdr=None) -> (host, port, token, (server, broadcast_task))`.
  Binds `host:0`; `token` is `secrets.token_urlsafe(32)`. `herdr` is injectable for tests.

- [ ] **Step 1: Write the failing test**

In `tests/test_local_mode.py`, add:

```python
import asyncio
from herdeck.bridge import StubHerdr, start_local_bridge
from herdeck.config import ServerConfig
from herdeck.connector import Connector


async def test_start_local_bridge_serves_snapshot_to_connector():
    herdr = StubHerdr([
        {"pane_id": "p1", "agent": "claude", "agent_status": "working",
         "foreground_cwd": "/proj/api", "workspace_id": "w1"},
    ])
    host, port, token, (server, btask) = await start_local_bridge(
        "/nonexistent.sock", herdr=herdr)
    got = asyncio.Event()
    seen = []
    conn = Connector(
        ServerConfig("local", f"ws://{host}:{port}", token),
        on_snapshot=lambda sid, st: (seen.extend(st), got.set()),
        on_event=lambda sid, s: None,
        on_connection=lambda sid, up: None,
    )
    run = asyncio.create_task(conn.run())
    try:
        await asyncio.wait_for(got.wait(), timeout=5)
        assert seen[0].agent_type == "claude"
        assert seen[0].label == "api"
    finally:
        conn.stop()
        btask.cancel()
        server.close()
        await server.wait_closed()
        run.cancel()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_local_mode.py::test_start_local_bridge_serves_snapshot_to_connector -v`
Expected: FAIL with `ImportError: cannot import name 'start_local_bridge'`.

- [ ] **Step 3: Implement `start_local_bridge`**

In `src/herdeck/bridge.py`, add near `serve`:

```python
async def start_local_bridge(socket_path, host="127.0.0.1", herdr=None):
    """Bind an embedded bridge on a loopback ephemeral port with a random,
    in-memory token. Returns (host, port, token, (server, broadcast_task))."""
    import secrets

    token = secrets.token_urlsafe(32)
    herdr = herdr or SocketHerdr(socket_path)
    events = HerdrEvents(herdr, socket_path=socket_path)   # push + slow poll
    clients: set = set()

    async def handler(ws):
        await _serve_connection(ws, herdr, "local", token, clients)

    server = await websockets.serve(handler, host, 0)
    port = server.sockets[0].getsockname()[1]
    btask = asyncio.create_task(_broadcast(events.stream(), clients, "local"))
    return host, port, token, (server, btask)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_local_mode.py::test_start_local_bridge_serves_snapshot_to_connector -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/bridge.py tests/test_local_mode.py
git commit -m "feat(bridge): start_local_bridge (embedded loopback bridge)"
```

---

### Task 6: `local_config` + `_run_local`

**Files:**
- Modify: `src/herdeck/app.py`
- Test: `tests/test_local_mode.py`

**Interfaces:**
- Consumes: `start_local_bridge` (Task 5), `_run` (existing), `config.DEFAULT_PROFILES`,
  `config.DEFAULT_MACROS`, `config.DEFAULT_START_PROFILES`.
- Produces:
  - `herdeck.app.local_config(port, token, partial=None) -> Config` — one `local`
    server at `ws://127.0.0.1:{port}`, profiles = defaults merged with `partial`'s.
  - `async herdeck.app._run_local(socket_path, deck, partial=None)` — starts the
    embedded bridge then runs the normal connector path against `local_config`.

- [ ] **Step 1: Write the failing tests (`local_config`)**

In `tests/test_local_mode.py`, add:

```python
from herdeck.app import local_config
from herdeck.config import Config, DEFAULT_PROFILES


def test_local_config_defaults():
    cfg = local_config(9999, "tok")
    assert cfg.servers[0].id == "local"
    assert cfg.servers[0].url == "ws://127.0.0.1:9999"
    assert cfg.servers[0].token == "tok"
    assert cfg.overview_order == ["local"]
    assert cfg.profiles["default"].approve == ["enter"]


def test_local_config_merges_partial_profiles():
    from herdeck.config import AnswerProfile
    partial = Config(servers=[], profiles={"claude": AnswerProfile(["x"], ["y"],
                     ["z"], ["x"])}, overview_order=[], grid=(5, 3))
    cfg = local_config(1, "t", partial)
    assert cfg.profiles["claude"].approve == ["x"]      # from partial
    assert cfg.profiles["default"].approve == ["enter"]  # default still present
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_local_mode.py -k local_config -v`
Expected: FAIL with `ImportError: cannot import name 'local_config'`.

- [ ] **Step 3: Implement `local_config` and `_run_local`**

In `src/herdeck/app.py`:

```python
def local_config(port, token, partial=None):
    """Synthesize the config for local mode from the bound bridge port/token."""
    from .config import (Config, DEFAULT_MACROS, DEFAULT_PROFILES,
                         DEFAULT_START_PROFILES, ServerConfig)
    profiles = dict(DEFAULT_PROFILES)
    if partial is not None:
        profiles.update(partial.profiles)
    return Config(
        servers=[ServerConfig("local", f"ws://127.0.0.1:{port}", token)],
        profiles=profiles,
        overview_order=["local"],
        grid=partial.grid if partial else (5, 3),
        macros=partial.macros if partial else list(DEFAULT_MACROS),
        start_profiles=(partial.start_profiles if partial
                        else dict(DEFAULT_START_PROFILES)),
    )


async def _run_local(socket_path, deck, partial=None):
    from .bridge import start_local_bridge
    host, port, token, _handle = await start_local_bridge(socket_path)
    await _run(local_config(port, token, partial), deck)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_local_mode.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/app.py tests/test_local_mode.py
git commit -m "feat(app): local_config and _run_local"
```

---

### Task 7: Wire `main()` to the new modes

**Files:**
- Modify: `src/herdeck/app.py` (`main`, add `_discover_config_path`)
- Test: `tests/test_local_mode.py`

**Interfaces:**
- Consumes: `resolve_mode`, `make_deck`, `local_config`, `_run`, `_run_local`,
  `_run_mock`, `_mock_config`, `load_config`.
- Produces: `herdeck.app._discover_config_path() -> str | None`
  (`HERDECK_CONFIG`, else `~/.config/herdeck/config.toml`, else `./config.toml`, else None).

- [ ] **Step 1: Write the failing tests (`_discover_config_path`)**

In `tests/test_local_mode.py`, add:

```python
from herdeck.app import _discover_config_path


def test_discover_prefers_env(monkeypatch, tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("")
    monkeypatch.setenv("HERDECK_CONFIG", str(p))
    assert _discover_config_path() == str(p)


def test_discover_none_when_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("HERDECK_CONFIG", raising=False)
    monkeypatch.setattr("os.path.exists", lambda p: False)
    assert _discover_config_path() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_local_mode.py -k discover -v`
Expected: FAIL with `ImportError: cannot import name '_discover_config_path'`.

- [ ] **Step 3: Implement `_discover_config_path` and rewrite `main()`**

In `src/herdeck/app.py`:

```python
def _discover_config_path():
    import os
    p = os.environ.get("HERDECK_CONFIG")
    if p:
        return os.path.abspath(p)
    for cand in (os.path.expanduser("~/.config/herdeck/config.toml"),
                 os.path.abspath("config.toml")):
        if os.path.exists(cand):
            return cand
    return None


def main() -> None:
    import os
    import sys

    if os.environ.get("HERDECK_DEBUG"):
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(levelname)s %(message)s",
        )
    mock = bool(os.environ.get("HERDECK_MOCK"))
    config_path = None if mock else _discover_config_path()
    file_config = load_config(config_path) if config_path else None  # before deck chdir (R-4)
    socket_path = os.path.expanduser(
        os.environ.get("HERDR_SOCKET", "~/.config/herdr/herdr.sock"))
    mode = resolve_mode(
        mock=mock, config_path=config_path,
        config_has_servers=bool(file_config and file_config.servers),
        socket_path=socket_path, socket_exists=os.path.exists(socket_path))
    if mode[0] == "error":
        print(mode[1], file=sys.stderr)
        sys.exit(2)

    grid = file_config.grid if file_config else (5, 3)
    slots = grid[0] * grid[1] - 2
    kind = os.environ.get("HERDECK_DECK") or (
        "fake" if os.environ.get("HERDECK_FAKE_DECK") else None)
    deck = make_deck(kind, slots)
    try:
        if mode[0] == "mock":
            asyncio.run(_run_mock(_mock_config(), deck))
        elif mode[0] == "remote":
            asyncio.run(_run(file_config, deck))
        else:  # local
            asyncio.run(_run_local(mode[1], deck, file_config))
    finally:
        deck.close()
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (all). Confirm no existing test regressed.

- [ ] **Step 5: Manual verification (local, no config)**

With herdr running locally and no `~/.config/herdeck/config.toml` and no
`HERDECK_CONFIG`:

```bash
HERDECK_DECK=web .venv/bin/python -m herdeck.app
```

Expected: prints the web simulator URL and serves the live agents (no token /
config / separate bridge). Then verify the error path:

```bash
HERDR_SOCKET=/nope.sock HERDECK_DECK=web .venv/bin/python -m herdeck.app; echo "exit=$?"
```

Expected: actionable stderr message, `exit=2`.

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/app.py tests/test_local_mode.py
git commit -m "feat(app): auto-detect local herdr; zero-config run"
```

---

### Task 8: README quick start (local)

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a "Quick start (local)" section**

Insert after the intro, before "Architecture":

```markdown
## Quick start (local)

If herdr runs on the same machine as your deck, no config or token is needed:

1. `pip install -e ".[deck]"` (Mac, real D200) or `pip install -e ".[dev]"`
   (web simulator only).
2. Make sure herdr is running (socket at `~/.config/herdr/herdr.sock`).
3. Run it:
   ```bash
   herdeck                 # drives an attached Stream Deck
   HERDECK_DECK=web herdeck # browser simulator at http://127.0.0.1:8800
   ```

herdeck auto-detects the local herdr socket and starts an embedded loopback
bridge for you. If no Stream Deck is attached it falls back to the web
simulator and prints its URL. Set `HERDR_SOCKET` if herdr's socket lives
elsewhere. For a remote deck (herdr on another host) see **Server setup**
below — that path uses an explicit config with `[[servers]]` and a token.
```

- [ ] **Step 2: Verify it renders** (read the file; no code test).

Run: `.venv/bin/python -m pytest -q` (sanity: nothing broke).
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: quick start for zero-config local mode"
```

---

## Self-Review

**Spec coverage:**
- Mode resolution (spec §1) → Task 3 (`resolve_mode`) + Task 7 (`main` wiring, XDG discovery, error exit).
- Optional servers/profiles (spec §2) → Task 1 (`DEFAULT_PROFILES`) + Task 2 (`load_config`).
- Embedded bridge (spec §3) → Task 5 (`start_local_bridge`).
- Local run path (spec §4) → Task 6 (`local_config`, `_run_local`).
- Deck fallback (spec §5) → Task 4 (`make_deck`).
- Error handling (spec §6) → Task 3 error tuple + Task 7 stderr/exit.
- Security (spec §7) → Task 5 (loopback bind, in-memory token).
- Testing (spec §Testing) → tests in Tasks 1–7.
- Docs (spec §Docs) → Task 8.

**Placeholder scan:** none — every code step has full code; no TBD/TODO.

**Type consistency:** `resolve_mode` signature identical in Task 3 def and Task 7
call. `make_deck(kind, slots, *, d200_factory, web_factory)` identical in Task 4
and Task 7. `start_local_bridge(...) -> (host, port, token, (server, btask))`
matches its Task 5 test and the Task 6 `_run_local` use (which ignores the
handle as `_handle`). `local_config(port, token, partial=None)` matches Task 6
def/tests and Task 7 call. `_run(config, deck)` reused unchanged.

**Note on R-4 ordering:** `main()` loads config before `make_deck` so the D200
driver's `os.chdir` cannot affect config path resolution.
