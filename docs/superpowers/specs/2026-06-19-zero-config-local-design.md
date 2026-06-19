# Zero-config local onboarding — design

- **Date:** 2026-06-19
- **Status:** Approved (brainstorming)
- **Sub-project of:** open-source readiness (goal: adoption)

## Goal

Make `herdeck` "just work" for the most common case — herdr running on the
same machine as the deck — with **no config file, no token, and no separately
started bridge**. The user installs herdeck, makes sure herdr is running, and
runs `herdeck`. If a Stream Deck is attached it drives it; otherwise it falls
back to the browser simulator and prints its URL.

Today this is impossible: `main()` always calls `load_config()`, which raises
without a config file and without a `[[servers]]` block, and the only data path
is the WebSocket `Connector` pointed at a manually started, token-authenticated
bridge.

## Non-goals

- Remote / multi-server setup. Unchanged: an explicit config with `[[servers]]`
  still selects the existing WebSocket path. No regressions there.
- Elgato / non-Ulanzi hardware (separate sub-project C).
- `herdeck init` wizard. Auto-detection (below) makes it unnecessary for v1.
- Decoupling from herdr.

## Design

### 1. Mode resolution (`main()`)

Resolution is a **pure function** over facts that `main()` has already gathered
(so every branch is unit-testable without touching the real filesystem):

```python
def resolve_mode(*, mock: bool, config_path: str | None,
                 config_has_servers: bool, socket_path: str,
                 socket_exists: bool): ...
```

`main()` does the IO (env lookups, file-existence checks, and loading the config
when a path is found) and passes plain values in. The function returns one of
`("mock",) | ("remote", config_path) | ("local", socket_path) | ("error", message)`.

Precedence:

1. `HERDECK_MOCK` set → `mock` (unchanged).
2. Resolve config path: `HERDECK_CONFIG` if set; else `~/.config/herdeck/config.toml`
   if it exists; else `./config.toml` if it exists. (Adds the XDG path to discovery.)
3. A config file is found **and declares `[[servers]]`** → `remote` (existing `_run`).
4. Else resolve the herdr socket: `HERDR_SOCKET` if set, else
   `~/.config/herdr/herdr.sock`. If it exists → `local`.
   A config file with no `[[servers]]` (profiles/macros/start_profiles only) is
   still loaded in this mode and its values override the built-in defaults.
5. Else → `error` with an actionable message, e.g.:
   `"No herdr socket at <path> and no [[servers]] config. Is herdr running? Set HERDR_SOCKET or create a config (see config.example.toml)."`
   `main()` prints it to stderr and exits non-zero.

### 2. Config: servers and profiles become optional

- Extract the built-in answer profiles (currently inline in `app._mock_config`)
  into a shared `DEFAULT_PROFILES` constant in `config.py`
  (`claude`/`codex`/`default`). `_mock_config` then reuses it.
- `load_config` changes:
  - `[[servers]]` optional → `servers=[]` when absent (no longer raises). The
    `remote` caller (`_run`) is responsible for requiring at least one server.
  - `answer_profiles` optional → falls back to `DEFAULT_PROFILES`. A config may
    override individual profiles; missing ones come from the defaults (merge, not
    replace). The previously required `answer_profiles.default` is supplied by the
    defaults, so it is no longer mandatory in the file.
  - `macros` / `start_profiles`: unchanged semantics (an explicit, even empty,
    section disables the built-ins; a missing section uses them).
- Local mode synthesizes a `Config` after the embedded bridge binds:
  `servers=[ServerConfig("local", "ws://127.0.0.1:<port>", <auto-token>)]`,
  `profiles` = merged defaults, `overview_order=["local"]`, `grid=(5,3)`,
  plus macros/start_profiles from the partial config (or defaults).

### 3. Embedded bridge (`bridge.py`)

New helper:

```python
async def start_local_bridge(socket_path: str, host: str = "127.0.0.1"):
    """Bind a loopback bridge on an ephemeral port with a random in-memory
    token. Returns (host, port, token, serve_task)."""
```

- `token = secrets.token_urlsafe(32)` (never written to disk).
- `herdr = SocketHerdr(socket_path)`, `events = HerdrEvents(herdr, socket_path=socket_path)`.
- `server = await websockets.serve(handler, host, 0)`; read the bound port from
  `server.sockets[0].getsockname()[1]`.
- Start `_broadcast(events.stream(), clients, "local")` as a task.
- The handler reuses `_serve_connection` (token auth via constant-time compare,
  identical to the remote bridge).

This binds 127.0.0.1 only — unreachable off-host — so the random token is a
defence-in-depth measure, not the primary control.

### 4. Local run path (`app.py`)

`_run_local(socket_path, deck, partial_config=None)` (the partial config is the
`Config` parsed from a serverless config file, or `None` when no file exists):

1. `host, port, token, _ = await start_local_bridge(socket_path)`.
2. Build the synthesized local `Config` (section 2) from the bound port/token and
   `partial_config`.
3. Reuse the existing connector wiring from `_run` against that config (one
   `Connector` to `ws://127.0.0.1:<port>`). The embedded bridge serve/broadcast
   tasks run under the same `asyncio.gather` as the connector and ticker.

To avoid duplicating `_run`, factor its connector-wiring body into a helper that
both `_run` and `_run_local` call, passing in any extra background tasks (the
embedded bridge's serve task for local).

### 5. Deck selection with auto-fallback (`main()`)

- `HERDECK_DECK` set (`d200`/`web`/`fake`) → honor exactly, no fallback.
- `HERDECK_DECK` unset:
  - Default attempt is `d200`. Wrap `D200Driver()` construction in try/except.
  - On failure (no device, or interface held by Ulanzi Studio), print the
    underlying reason plus a hint
    (`"No Stream Deck opened (close Ulanzi Studio if it's running). Falling back to the web simulator."`)
    and construct `WebDeck`, printing its URL.
- `HERDECK_DECK=d200` explicitly and it fails → error out (no silent fallback;
  the user asked for the hardware).

Deck selection is independent of the herdr mode (local/remote/mock).

### 6. Error handling

- Mode `error` (no socket, no servers) → friendly stderr message + non-zero exit.
- Embedded bridge cannot reach the herdr socket at runtime → `SocketHerdr` RPCs
  fail and the connector reports the server down (existing red/OFFLINE behavior);
  emit a one-time log hint that herdr may not be running.
- A failure in the embedded bridge serve task must not deadlock the app; it is
  guarded like the other tasks.

### 7. Security

- Loopback-only bind (`127.0.0.1`) + random in-memory token. No port exposed off
  the host, no secret persisted. Remote mode is unchanged (token + Tailscale bind).

## Testing strategy (TDD)

Unit:
- `resolve_mode` — all branches: mock; remote (config with servers); local
  (no servers + socket); local with partial config; error (no socket, no servers).
  Filesystem/env effects injected as parameters.
- `load_config` — no `[[servers]]` → `servers==[]`; no `answer_profiles` →
  `DEFAULT_PROFILES`; partial `answer_profiles` merges over defaults; existing
  remote configs still parse unchanged.
- Deck fallback — inject a `d200_factory` that raises; assert a `WebDeck` is
  returned; assert an explicit `HERDECK_DECK=d200` failure raises instead.

Integration:
- `start_local_bridge` against a `StubHerdr` exposed over a temporary unix socket
  (or `SocketHerdr` pointed at a stub server): connect a real `Connector` and
  assert it receives the initial snapshot and that an `act`/`read` round-trips.

Regression:
- Full existing suite stays green (notably the existing remote/`_run`,
  orchestrator, and bridge tests).

## Docs

- README: add a "Quick start (local)" section — run herdr, `pip install -e .`,
  `herdeck`; note the web-simulator fallback and how to force a deck with
  `HERDECK_DECK`. (The full README rework + screenshots is sub-project A.)

## Files touched

- `src/herdeck/config.py` — `DEFAULT_PROFILES`; optional servers/profiles.
- `src/herdeck/bridge.py` — `start_local_bridge`.
- `src/herdeck/app.py` — `resolve_mode`, `_run_local`, shared connector wiring,
  reuse `DEFAULT_PROFILES` in `_mock_config`, deck fallback.
- `tests/` — new `test_local_mode.py` (resolve_mode, deck fallback,
  start_local_bridge) + `test_config.py` additions.
- `README.md` — Quick start (local).

## Out of scope / follow-ups

- Elgato hardware (sub-project C), `herdeck init`, herdr-decoupling,
  full README/screenshots (sub-project A), PyPI publish (sub-project A).
