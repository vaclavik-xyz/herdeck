# herdeck desktop app — Phase 3c onboarding (first-run připojení) — design

- **Date:** 2026-06-29
- **Status:** Approved (brainstorming)
- **Sub-project of:** herdeck desktop app, Phase 3 (distribution & polish), slice 3c
- **Split:** **3c-i** (backend) + **3c-ii** (frontend) — own plan each, shared `/setup` contract defined here

## Goal

Make the first launch of the installed `.app` understandable and connectable.
Today a fresh install with no `~/.config/herdeck/config.toml` silently shows the
**mock deck** (fake claude/codex/… agents): the user has no signal it is demo
data, nor how to reach their real agents. This slice replaces that silent mock
on first run with a guided card in the floating-deck window that connects the app
to a herdr bridge — either a **locally running herdr** (auto-detected, embedded
bridge, zero config) or a **remote herdr** (URL + token) — and remembers the
choice so subsequent launches connect on their own.

## Non-goals

- Multi-server onboarding. v1 connects exactly one server (matches the deckapp's
  single-Connector `LiveSource`). Adding more is the existing config editor's job.
- Replacing the config editor. The editor stays the full/advanced surface; the
  onboarding card is the minimal first-run path. Anything the card writes is
  editable there afterwards.
- Changing the herdeck **CLI** (`app.py`) run paths. The CLI already does
  mock/remote/local via `resolve_mode`; this slice ports the *local* capability
  into the **deckapp sidecar**, reusing the same building blocks, without touching
  the CLI's own startup.
- Windows. (herdeck has no Windows deck driver; Phase 3 non-goal.)
- A multi-step "wizard" with profile/notification setup. v1 is a single card whose
  only job is to get connected; everything else lives in the editor.

## First-run model

### Source-selection precedence (deckapp sidecar)

The sidecar today resolves **mock** vs **remote** (`select_live()`); this slice
adds **local** and an explicit **onboarding state**. New precedence:

| # | Condition | Result (reason) |
|---|-----------|-----------------|
| 1 | `HERDECK_MOCK` set | `mock` (`mock_env`) — unchanged |
| 2 | remote config (`[[servers]]` + resolvable token) | `remote` — unchanged `select_live` |
| 3 | persisted choice `local` **and** herdr socket exists | **`local`** (embedded bridge) |
| 4 | persisted choice `local` **and** socket missing | `mock` (`local_unavailable`) → reconnect card |
| 5 | persisted choice `demo` | `mock` (`demo`) — no card |
| 6 | otherwise (never onboarded) | `mock` (`first_run`) → welcome card |

Remote (case 2) is implied by the presence of a usable `config.toml`, so it needs
no marker. Only the `local`/`demo` opt-ins are persisted. The cases map onto
`select_source_kind`'s `("mock", reason)` return, and the reason drives which card
(if any) the frontend shows.

### Persisted onboarding state

A small file next to the config: `<config_dir>/onboarding.toml`, where
`<config_dir>` is the directory of the discovered config path, else
`~/.config/herdeck/`. Honors `HERDECK_CONFIG` (its parent dir) for test isolation.

```toml
choice = "local"   # or "demo"
```

- Absent file → never onboarded.
- Atomic write (tmp + `os.replace`, like `ConfigService._atomic_write`).
- `tomllib` (stdlib, Python ≥3.12) read, `tomli_w` write (already a dependency).

## Architecture overview

```
                         deckapp sidecar (Python)
  ┌───────────────────────────────────────────────────────────────┐
  │  select_source_kind()  ──▶ ("remote", cfg, srv)                │
  │     (precedence 1–5)       ("local", socket_path)              │
  │                            ("mock", reason)                    │
  │                                                                │
  │  local ▶ LocalBridgeRunner ─(loopback ws)─▶ build_live_source  │
  │           (start_local_bridge on its own asyncio loop/thread)  │
  │                                                                │
  │  HTTP:  GET /setup            POST /setup/connect              │
  └───────────────────────────────────────────────────────────────┘
        ▲ token-injecting Rust proxies (setup_status / setup_connect)
  ┌───────────────────────────────────────────────────────────────┐
  │  App.svelte ── reason→card? ─▶ <Onboarding>  ──connect──▶ deck │
  └───────────────────────────────────────────────────────────────┘
```

- **3c-i (backend):** the embedded local bridge in the sidecar, source-selection
  precedence + onboarding-state persistence, the two `/setup*` HTTP routes, the
  two Rust proxy commands, and the frozen/packaging wiring. Independently testable
  via pytest (`StubHerdr`) + cargo; mergeable without any frontend.
- **3c-ii (frontend):** the `<Onboarding>` card in the floating-deck window, the
  setup-status poll + flip-to-deck, and the local/remote/demo flows.

---

## 3c-i — Backend

### LocalBridgeRunner (embedded bridge in the sidecar)

A small lifecycle owner mirroring `live.ConnectorRunner`: it owns one asyncio loop
on a daemon thread and runs the embedded bridge there. The bridge and the
`Connector` run on **two separate loops/threads**, talking only over the loopback
WebSocket — no shared-loop coupling.

```python
class LocalBridgeRunner:
    def __init__(self, socket_path: str, *, start_bridge=start_local_bridge): ...
    def start(self) -> tuple[str, int, str]:
        """Run start_local_bridge on the runner's loop; block until bound.
        Returns (host, port, token)."""
    def close(self) -> None:
        """Cancel the broadcast task, close the ws server, stop the loop."""
```

- `start()` schedules `start_local_bridge(socket_path)` on the runner loop, waits
  (threadsafe) for `(host, port, token, handle)`, stashes `handle=(server, btask)`,
  returns `(host, port, token)`.
- `close()` cancels `btask`, closes `server`, awaits `wait_closed`, stops the loop,
  joins the thread (bounded). Idempotent + exception-guarded (matches existing
  close() patterns).
- `start_bridge` is injectable so tests pass `start_local_bridge(..., herdr=StubHerdr([...]))`
  via a partial — no real herdr or unix socket needed.

### Local source selection + lifecycle

- New `select_source_kind()` in `deckapp/server.py` returns one of:
  - `("remote", config, server)` — current `select_live()` success.
  - `("local", socket_path)` — precedence case 3.
  - `("mock", reason)` — reason ∈ `{"mock_env","demo","first_run","local_unavailable"}`.
  `select_live()` stays as the remote sub-decision it already is; the new function
  layers the onboarding state + socket detection on top.
- The herdr socket path is resolved by a shared helper (`HERDR_SOCKET` env →
  `config.hardware.herdr_socket` → `~/.config/herdr/herdr.sock`). `app._resolve_socket_path`
  is the existing logic; factor it to a shared module (e.g. `bootstrap.resolve_socket_path`)
  and have both the CLI and the deckapp call it — no behavior change for the CLI.
- `create_app()` builds the right source:
  - `local` → start a `LocalBridgeRunner`, `(host, port, token) = runner.start()`,
    `cfg = local_config(port, token, partial_config)`, `src = build_live_source(cfg, cfg.servers[0])`.
    Track the runner on the app (`app._local_bridge`) and close it in `DeckApp.close()`
    alongside `_watcher`/`_source`.
  - `remote`/`mock` → unchanged.
- **Bridge lifecycle lives in two places only:** (a) `create_app` startup (case 3),
  and (b) `POST /setup/connect`. A helper `DeckApp._set_local_bridge(runner)` closes
  any previous runner before storing the new one, so switching away (connect
  remote/demo) tears the bridge down. The existing config-file watcher/reloader is
  **unchanged** (it serves remote edits; pure-local mode usually has no config files
  to watch), avoiding bridge churn on unrelated reloads.

### Onboarding state module

A focused helper (e.g. `deckapp/onboarding.py`), no IO logic reimplemented elsewhere:

```python
def state_path(config_path: str | None) -> Path        # <config_dir>/onboarding.toml
def read_choice(config_path) -> str | None             # "local" | "demo" | None
def write_choice(config_path, choice: str) -> None      # atomic; validates the enum
def clear_choice(config_path) -> None                   # remove the marker (idempotent)
```

### HTTP contract: `/setup` and `/setup/connect`

Both authenticate exactly like the existing routes (GET → `?token=`, POST →
`X-Herdeck-Token` header; 403 body is the shared `_FORBIDDEN`).

**`GET /setup` → 200 JSON** (the frontend's single source of first-run truth):

```jsonc
{
  "mode": "mock" | "local" | "remote",
  "connected": false,                 // live feed up? (false for mock)
  "reason": "first_run",              // why mock: mock_env|demo|first_run|local_unavailable; null when live
  "local_herdr_available": true,      // herdr socket exists right now
  "choice": "local" | "demo" | null,  // persisted opt-in
  "socket_path": "/Users/…/herdr.sock"
}
```

**`POST /setup/connect` (header token)** — the one runtime path that changes mode.
Body `{ "choice": "local" | "remote" | "demo", … }`:

- `{"choice":"local"}` → start the embedded bridge (`LocalBridgeRunner`), swap the
  source to the live source over it, `write_choice("local")`. Returns
  `{"ok": true, "connected": <bool>}`. If the socket is gone → `{"ok": false, "error": "herdr socket not found at <path>"}` (nothing persisted).
- `{"choice":"remote", "url": "...", "token": "...", "id": "..."}` →
  **test-before-commit**: probe the server (below). On success, persist in a
  **secret-before-config** order so no half-written state is observable:
  1. `set_secret(token_env, token)` (keychain) — if this raises, **nothing else is
     written** (no config), the source is unchanged, and the call returns
     `{"ok": false, "error": "could not store token"}`.
  2. read the current config payload (`ConfigService.read`), **upsert** the server
     into `base.servers` (match by `id`: replace in place, else append), and
     `ConfigService.write` the **merged** payload (`{base, profiles, local}`).
     Reading-then-merging preserves every other base/profile/local section, so
     reconnecting an existing-but-currently-unusable config never drops profiles,
     theme, macros, or local hardware settings. (First run: `read()` returns empty
     sections, so the upsert simply creates the one server.)
  3. **verify before claiming success:** re-resolve the just-written config through
     the sidecar's own selection (`select_source_kind` / `select_live`) and require
     it to yield a `remote` source whose selected server is the one just configured
     (its token resolves). Only then swap to that source, `clear_choice()` the stale
     marker, and return `{"ok": true, "connected": true}`.

     If resolution does **not** select that server — e.g. a non-default active
     profile or `deck.overview_order` constrains the resolved `servers` so
     `config.servers[0]` is a different, still-tokenless server — return
     `{"ok": false, "error": "server saved, but the active profile/overview_order
     doesn't select it — fix it in Settings"}` and do **not** swap. The secret +
     config stay written (editable in the config editor); onboarding never reports a
     connect it cannot back up. The default / first-run path always selects the lone
     base server, so this only triggers for pre-existing constrained configs.

  Returns `{"ok": true, "connected": true}`. On probe failure: return
  `{"ok": false, "error": "<reason>"}` and **persist nothing** (no secret, no config,
  marker untouched). Secret-first guarantees the persisted config's `token_env`
  already resolves the instant the file appears, so a watcher-triggered reload in
  the gap can never observe a serverful-but-tokenless config. `id` defaults to
  `"herdr"`; `token_env` derives deterministically from `id`
  (e.g. `HERDECK_<ID>_TOKEN`, upper-cased, non-alnum→`_`).

  **State coherence:** clearing the marker on remote success keeps the model
  single-valued — *remote == a usable `config.toml`, no opt-in marker*. So if that
  remote config later becomes unusable or is removed, precedence falls through to
  case 6 (`first_run`) and the welcome card resurfaces the problem, rather than a
  stale `demo`/`local` marker silently masking it.
- `{"choice":"demo"}` → `write_choice("demo")`, ensure the mock source, return
  `{"ok": true}`.

400 for a malformed body / missing required fields / unknown `choice` (mirrors the
existing `_json_body` + field-check pattern). `/setup*` 404 only the way other
config routes do when no config service is wired.

### Server probe (remote test-before-commit)

A small standalone async probe — no full Connector spin-up:

```python
async def probe_server(url: str, token: str, *, timeout: float = 4.0) -> ProbeResult
# connects ws(s)://… with Authorization: Bearer <token>;
#   first frame is a snapshot      → ok
#   server closes 4401             → bad_token
#   refused / timeout / bad url    → unreachable
```

Reuses `websockets.connect`; `ProbeResult` carries `ok: bool` + a stable
`reason` string for the UI. Testable by probing a `start_local_bridge` instance
backed by `StubHerdr` (valid token → ok; wrong token → bad_token).

**Timeout budget.** The probe (4 s) runs *inside* the sidecar's `/setup/connect`
handler, so the `setup_connect` Rust proxy must use a **dedicated timeout strictly
longer than the probe** (≈ 8 s) — **not** the shared 3 s `SIDECAR_TIMEOUT` the
other proxies use. Otherwise a slow-but-valid probe would make the Tauri command
time out before the sidecar returns, while the sidecar still persists/swaps — a
torn result. `setup_status` keeps the default short timeout (it does no network I/O).

### Rust proxy commands

Two token-injecting commands mirroring the existing config proxies, registered in
`invoke_handler!`:

- `setup_status()` → `GET /setup?token=…` → JSON (query-token, like `config_read`;
  default short `SIDECAR_TIMEOUT` — no network I/O).
- `setup_connect(body)` → `POST /setup/connect` (header token, like `config_validate`),
  returns the JSON `{ok, connected?, error?}`. Uses a **dedicated ≈8 s timeout**
  (longer than the 4 s probe), not the shared 3 s `SIDECAR_TIMEOUT`. The token
  VALUE the user typed is in the POST body the Rust shell forwards; it is never
  read back (one-way, like `config_secret_set`).

### Frozen / packaging

The local path adds first-time imports of `herdeck.bridge`, `herdeck.bootstrap`,
and (already used) `herdeck.connector` — all stdlib + `websockets` (already
bundled); **no cairosvg or other heavy dep**. Add them to `hiddenimports` in
`desktop/herdeck-deckapp.spec` (belt-and-suspenders over PyInstaller's analysis).
The headless `smoke-sidecar.sh` cannot exercise a real local connection (no herdr),
so it adds only an **import-reachability** check (the frozen binary can import the
local-bridge path); full local behavior is covered by pytest with `StubHerdr`.

---

## 3c-ii — Frontend

### Onboarding card in the floating-deck window

`App.svelte` gains a setup-status poll (reusing the discovery-retry pattern). It
renders `<Onboarding>` instead of `<DeckView>` while the status says the user must
act; once connected it shows the deck.

Render decision from `GET /setup` status, **exhaustive on `reason`**:

- `reason === "first_run"` → full welcome card.
- `reason === "local_unavailable"` → reconnect card ("herdr neběží…", retry button)
  — the `local` opt-in is remembered but herdr is currently down.
- otherwise → the deck (`<DeckView>`). This covers live `local`/`remote`
  (`reason === null`, connected) **and** the deliberate-mock cases (`reason ===
  "mock_env"` forced via `HERDECK_MOCK`, or `reason === "demo"`) — no card, the
  mock deck shows. The default being "show the deck" guarantees no setup state can
  trap the user behind a card that does not apply.

The status is polled (a few seconds) so that after a successful
`setup_connect` — and after the source swap settles — the card flips to the deck
without a manual refresh.

### Card actions (welcome state)

- **Local** — shown prominently when `local_herdr_available`: `✓ herdr běží lokálně`
  + `[Připojit]` → `setup_connect({choice:"local"})`. On `{ok:false}` show the error
  inline.
- **Vzdálený herdr…** — expands a small form: `URL` (`ws(s)://…`) + `Token` +
  optional `id`. `[Připojit]` → `setup_connect({choice:"remote", url, token, id})`.
  On `{ok:false, error}` show the reason inline (bad token / unreachable / bad url).
  The token field is a plain password input; its value goes straight into the
  command and is never read back.
- **Prozkoumat demo** — `setup_connect({choice:"demo"})` → flips to the (mock) deck.

When `local_herdr_available` is false, the card leads with the remote form and a
hint that herdr was not detected locally (start herdr, or connect remotely).

### Component boundaries

- `src/lib/onboardingClient.ts` — framework-free parse/normalize of the `/setup`
  status + the three `setup_connect` request shapes (Vitest-tested, mirrors
  `configClient.ts`/`sidecar.ts` style).
- `src/lib/Onboarding.svelte` — the card; pure props/events, no direct `invoke`
  beyond the two setup commands via an injected transport (testable seam like the
  existing sections).
- `App.svelte` — wires status polling + the `<Onboarding>` vs `<DeckView>` switch.

## Security

- The sidecar still binds loopback only; `/setup*` reuse the same constant-time
  token auth as every other route. The access token never reaches JS (Rust injects
  it), exactly as the deck/config proxies.
- The embedded local bridge binds `127.0.0.1` on an ephemeral port with a random
  in-memory token (defence-in-depth; unreachable off-host), identical to the CLI's
  `start_local_bridge`.
- Remote secret VALUES are one-way: the typed token flows into `set_secret`
  (keychain) / the embedded auth header and is never returned, logged, or written
  to TOML. The config stores only the `token_env` NAME + `{set, source}` flags,
  as everywhere else. Keyring service literal stays `"herdeck"`.

## Testing strategy (HW-free)

**3c-i (pytest + cargo):**
- `select_source_kind` — all 6 precedence cases (incl. `local_unavailable` and
  `first_run`); env/fs/choice injected as parameters (pure decision over gathered facts).
- `LocalBridgeRunner` — `start()` against `start_local_bridge(..., herdr=StubHerdr([…]))`:
  a real `Connector` (or `probe_server`) connects over the bound loopback port and
  receives the initial snapshot; `close()` tears the bridge down (no lingering task).
- onboarding-state module — `read_choice`/`write_choice` round-trip; absent file →
  `None`; atomic write; `HERDECK_CONFIG` redirection.
- `/setup` — JSON shape for each mode (mock/first_run/demo/local/remote).
- `/setup/connect` — `local` (starts bridge, swaps, persists), `remote` ok
  (probe ok → secret-then-config written + swaps + marker cleared), `remote` probe
  fail (nothing persisted, source unchanged), `remote` secret-store fail
  (`set_secret` raises → **no config written**, source unchanged, returns
  `{ok:false}`), `remote` selected-server mismatch (active profile / `overview_order`
  excludes the upserted server → `{ok:false}`, **no swap**, config stays as written),
  `demo` (persists, stays mock); 400s for bad bodies.
- `probe_server` — ok / bad_token / unreachable against a `StubHerdr`-backed bridge.
- Rust: `setup_status`/`setup_connect` proxy commands (token injection, status
  passthrough) in the existing `desktop/src-tauri/tests` style.

**3c-ii (Vitest + build):**
- `onboardingClient` — status parse/normalize (all render cases), the three request
  builders, error passthrough.
- `Onboarding.svelte` compile-smoke (the repo has no Svelte render harness — same
  accepted gap as the config sections; `npm run build` is the gate, no svelte-check).

**Regression:** full existing suite stays green (pytest, ruff `src tests`, cargo
test, `npm run build`), plus the real PyInstaller freeze + headless smoke gate from
3a (now with the import-reachability addition).

## Files touched

**3c-i:**
- `src/herdeck/bootstrap.py` — shared `resolve_socket_path` (moved from `app.py`).
- `src/herdeck/app.py` — call the shared `resolve_socket_path` (no behavior change).
- `src/herdeck/deckapp/local_bridge.py` — `LocalBridgeRunner`.
- `src/herdeck/deckapp/onboarding.py` — onboarding-state read/write.
- `src/herdeck/deckapp/probe.py` — `probe_server` / `ProbeResult`.
- `src/herdeck/deckapp/server.py` — `select_source_kind`, local branch in
  `create_app`/`_select_source`, `DeckApp._set_local_bridge` + close wiring, the
  `/setup` + `/setup/connect` routes.
- `desktop/src-tauri/src/lib.rs` — `setup_status`/`setup_connect` commands +
  registration.
- `desktop/herdeck-deckapp.spec` — `hiddenimports` for bridge/bootstrap/connector.
- `desktop/scripts/smoke-sidecar.sh` — import-reachability check for the local path.
- `tests/` — `test_deckapp_onboarding.py`, `test_deckapp_local_bridge.py`,
  `test_deckapp_setup_routes.py`, `test_probe.py` (+ `bootstrap` socket-path move
  coverage); `desktop/src-tauri/tests/` setup-proxy coverage.

**3c-ii:**
- `desktop/src/lib/onboardingClient.ts` (+ `.test.ts`).
- `desktop/src/lib/Onboarding.svelte`.
- `desktop/src/App.svelte` — status poll + card/deck switch.

## Out of scope / follow-ups

- Multi-server / profile onboarding (editor's job).
- Auto-detecting a remote herdr (Bonjour/Tailscale discovery). v1 remote is manual
  URL + token.
- A "demo" badge on the mock deck after the user explicitly chose demo (cosmetic).
- Reload-driven local↔remote transitions beyond the explicit `/setup/connect` path.
- The manual `.app` first-run gate (double-click on a Mac, confirm the card →
  connect → deck) — verified by the user, like every Phase 3 GUI gate.
