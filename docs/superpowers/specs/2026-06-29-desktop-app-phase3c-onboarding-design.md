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
| 1 | `HERDECK_MOCK` set | `mock` (`mock_env`) |
| 2 | persisted choice `local` **and** herdr socket exists | **`local`** (embedded bridge) |
| 3 | persisted choice `local` **and** socket missing | `mock` (`local_unavailable`) → reconnect card |
| 4 | persisted choice `demo` | `mock` (`demo`) — no card |
| 5 | remote config (`[[servers]]` + resolvable token) | `remote` (`select_live`) |
| 6 | otherwise (never onboarded) | `mock` (`first_run`) → welcome card |

An **explicit `local`/`demo` marker outranks a remote config** (cases 2–4 before
case 5). The marker is changed only by `/setup/connect`: a successful **remote** connect
CLEARS it (so a fresh remote setup has no marker → case 5 selects the config), while a
**demo/local** connect SETS it. So when no marker is present, a usable `config.toml`
selects remote; when a demo/local marker is present it outranks the config (which may
still be on disk), so an explicit demo/local choice sticks across restart. Only the
`local`/`demo` opt-ins are persisted. The cases map onto `select_source_kind`'s
`("mock", reason)` return, and the reason drives which card (if any) the frontend shows.

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
  - `("local", socket_path)` — precedence case 2.
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
- **Bridge lifecycle lives in two places only:** (a) `create_app` startup (case 2 — local),
  and (b) `POST /setup/connect`. A helper `DeckApp._set_local_bridge(runner)` closes
  any previous runner before storing the new one, so switching away (connect
  remote/demo) tears the bridge down. The config-file watcher/reloader is **mode-aware**
  (detailed under the connect-transaction section below): **both `create_app` startup AND
  each `/setup/connect`** install a reloader matching the mode via `_reloader_for(app, kind)`
  — so a deck launched from a persisted `local` choice gets the **no-op** reloader too,
  exactly like a connect-time local switch (**local → no-op** so a config-mtime reload can't
  swap the bridge out and orphan it; **remote/demo/mock → normal**). The reloader's
  `_select_source` re-selects through the marker-aware precedence (so a demo reload stays
  mock), and the remote connect transaction **suppresses the watcher + `resync`s its mtime
  baseline** so its own writes don't trigger a spurious reload mid-commit.

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

- `{"choice":"local"}` → run a **full transaction** (the bridge start is inside it too):
  snapshot the prior marker, `_start_local_bridge` (which closes its own partial runner on a
  bind failure), build the live source over it, `_prepare_swap` (orchestrator + dry-render —
  fallible) BEFORE the marker, `write_choice("local")`, then `_commit_swap` (non-failing). If
  the bridge start, build, prepare, OR marker write fails, **restore the prior marker**, close
  the built source AND the just-started bridge runner (each if created), and leave the
  previous source + bridge untouched (`{"ok": false, …}`) — so the runtime mode never changes
  without the choice being persisted, and vice-versa, and nothing leaks (the commit itself
  cannot fail). On success, adopt the new bridge and install the local (no-op) reloader.
  Returns
  `{"ok": true, "connected": <app._source.connected>}` (honest, connector dials async).
  If the socket is gone → `{"ok": false, "error": "herdr socket not found at <path>"}`
  (nothing persisted).
- `{"choice":"remote", "url": "...", "token": "...", "id": "..."}` →
  **test-before-commit** with **build-before-persist**: everything that can fail runs
  BEFORE any mutation, so **any `{ok:false}` leaves no durable state** (no orphaned
  secret, no serverful-but-dead config that would shadow onboarding next launch):
  1. **env-collision guard:** secret resolution is **env-first**, so if `token_env`
     (`HERDECK_<ID>_TOKEN`) is already exported with a value DIFFERENT from the typed
     token, that env value would shadow whatever is stored in the keychain and the
     persisted config would not resolve to the typed token. Reject up front:
     `{"ok": false, "error": "<token_env> is set in the environment and would override
     the saved token; unset it or connect with that value"}` (nothing touched). An equal
     env value is fine; an unset one means the keychain resolves.
  2. probe the server (below). Failure → `{"ok": false, "error": "<reason>"}`,
     nothing touched.
  3. build the merged payload **in memory**: read the current config
     (`ConfigService.read`) and **upsert** the server into `base.servers` (match by
     `id`: replace in place, else append). Reading-then-merging preserves every other
     base/profile/local section, so reconnecting an existing config never drops
     profiles, theme, macros, or local hardware. (First run: `read()` returns empty
     sections, so the upsert simply creates the one server.) An unreadable/malformed
     existing config (parse error, or a non-list/`["bad"]` `servers`) is rejected here as
     `{"ok": false}`. The derived `token_env` is `HERDECK_<ID>_TOKEN`, which lives in ONE
     flat keychain namespace shared by every config section. Collect every `token_env` the
     existing config references (`ConfigService._collect_token_envs` over base + profiles —
     other servers, `notifications.telegram`, profile overlays) EXCEPT the server being
     replaced, and **reject if the derived `token_env` is already in use** — two ids can
     collide (`foo-bar` / `foo_bar`) and a derived name can clash with a non-server secret,
     so connecting must never overwrite another section's keychain token.
  4. **resolve + verify selection, still in memory:**
     `ConfigService.resolve_selected_server(payload, assume_present=token_env)` resolves
     the active profile with **only the new token placeholdered** — every OTHER selected
     server's `token_env` must resolve for real. So a config that would NOT load on
     restart (another selected server's token missing) is rejected here, never persisted.
     Returns `(config, servers[0])`. If there is no server, the config doesn't resolve, or
     `servers[0].id` is not the configured `id` (a non-default active profile or
     `deck.overview_order` constrains the resolved `servers`) → return `{"ok": false,
     "error": "config does not resolve to this server … — fix it in Settings"}` and
     persist **nothing**. (The first-run path always selects the lone base server; this
     only triggers for pre-existing constrained/broken configs.)
  5. **build the live source AND pre-build the swap, BEFORE persisting:** bake the real
     token into the chosen server (`dataclasses.replace(server, token=token)`),
     `build_live_source`, and `_prepare_swap(new_source, clock=time.monotonic)` (construct
     the orchestrator AND **render** the tiles+panel, capturing the bytes — the fallible
     parts of a swap — **with the live clock** so elapsed-time text advances, not the
     mock's frozen clock). `build_live_source` starts a connector immediately, so if
     `_prepare_swap` (or anything after the source is built) raises, **close the built
     source** before returning `{"ok": false, "error": "could not build the remote
     source"}` — no leaked connector — still **nothing persisted**. Doing all fallible work
     up front means a repeatable failure can never leave a remote config on disk that would
     shadow onboarding next launch, and the post-persist commit (step 7) cannot throw.
  6. only now persist, **secret-then-config**, but **snapshot everything for rollback
     BEFORE any mutation**: `peek_keychain(token_env)` for the prior token (it **raises**,
     not returns `None`, on a keychain read error — so "missing" is distinguished from
     "unreadable" and a rollback never erases a token it couldn't snapshot), and the prior
     `config.toml`/`local.toml` text — a read fault (keychain OR config) here aborts with
     **nothing mutated** (no orphaned secret), **closing the built source** so its connector
     doesn't leak. Then `set_secret(token_env, token)` — on failure **restore the
     prior keychain value** (the set may have partially overwritten it), close the source,
     `{"ok": false}`. Then `ConfigService.write` the merged payload — if it **returns
     errors OR raises** (its two atomic writes, config then local, can fault, possibly
     after a partial write), **restore both files to the snapshot AND the keychain to its
     prior value** (re-store the snapshot if it existed, else clear), close the source,
     `{"ok": false}` (so a partial failure leaves neither a serverful-but-tokenless config
     nor a destroyed prior token). Secret-then-config means the config's `token_env`
     resolves the instant the file appears (no watcher-reload race).
  7. **finalize → swap → adopt:** `clear_choice()` the stale `local`/`demo` marker **as
     part of the commit** (the state model is *remote == a usable config, no opt-in
     marker*). If even this unlink faults, roll back the config + secret, **close the
     just-built source**, and return `{"ok": false}` — so a later-removed remote config
     always falls to `first_run` (the onboarding card), never to a stale marker that
     would mask it. (Every post-build failure — set_secret, write, marker-clear — closes
     the built source so its connector never leaks.) Then **`_commit_swap`** (the
     assignment-only commit — assigns the pre-rendered tiles from step 5, no render, so it
     cannot fail) to the built source, drop any local bridge, and return
     `{"ok": true, "connected": <app._source.connected>}` — the actual swap-time status,
     **never a hardcoded `true`** (the connector dials asynchronously, so it is usually
     `false` right after connecting; the frontend's `/setup` poll surfaces it flipping).
     `id` defaults to `"herdr"`; `token_env` derives deterministically from `id`
     (e.g. `HERDECK_<ID>_TOKEN`, upper-cased, non-alnum→`_`).

  Steps 6–7 run as **one transaction with the config watcher suppressed** (and its
  mtime baseline resynced on exit). The sidecar's `ConfigWatcher` polls `config.toml`/
  `local.toml` mtimes every second and fires `DeckApp.reload()`; without suppression it
  could fire in the gap between the config write and the swap — double-swapping to a
  second source on success, or swapping to the half-written config during a rollback,
  breaking the "source unchanged on failure" guarantee. The commit gates `reload()` via a
  `_suppress_reload` flag and calls `ConfigWatcher.resync()` on exit so the watcher adopts
  the just-written (or restored) files as its baseline and never fires on them.

  `DeckApp.swap_source` is split into **`_prepare_swap`** (build the orchestrator AND
  **render** the tiles+panel on a throwaway orchestrator, capturing the bytes — all the
  fallible parts) and **`_commit_swap`** (**assign** `self._source`/orchestrator/clock +
  the pre-rendered tiles under the lock — **pure assignment, no render**, so it cannot
  raise). `_refresh_locked` is correspondingly split into `_render_locked` (the fallible
  render) + `_apply_rendered_locked` (the pure version-diff + assign). The onboarding
  commit runs `_prepare_swap` BEFORE any durable write (step 5) and `_commit_swap` AFTER
  (step 7), so a malformed-config OR render failure aborts before persisting and the
  post-persist commit is provably non-throwing — no swap-rollback-of-durable-state path is
  ever needed. (The commit assigns the prepare-time render; the next bridge update
  refreshes to the latest — a negligible, self-healing staleness. `swap_source` stays a
  prepare+commit convenience for the reloader and other callers.)

  **Pre-commit connector isolation:** `build_live_source` starts the connector immediately,
  but the `LiveSource` is **not attached to the deck until `_commit_swap`** (the existing
  `LiveSource.attach()` wiring). Before attach, its connector callbacks (`on_snapshot` /
  `on_event` / …) only update the source's **own buffer** — they do NOT take the deck lock,
  re-render, or mutate `self._source`/tiles (the Phase-1 `LiveSource` guarantee, covered by
  `test_deckapp_live.py`). And `_prepare_swap` renders a **throwaway** orchestrator, never
  the deck's. So a connector callback firing between `build_live_source` and `_commit_swap`
  (e.g. just before a post-build failure) cannot touch live deck state — rollback leaves the
  previous source and its rendering fully intact.

  Durable-state mutations are **serialized** by a shared app-level `_setup_lock` (a
  **`threading.RLock`**): the whole `/setup/connect` flow holds it across `connect()`, every
  config-editor write route (`/config`, `/profiles/active`, `/secret`, DELETE `/secret`)
  holds it across its mutation, AND **`DeckApp.reload` takes it too** — `ThreadingHTTPServer`
  serves these concurrently, so without one lock a config edit could interleave with a
  connect's snapshot/write/rollback, two connects with each other, or a watcher-driven reload
  could fire mid-connect and observe the **placeholder token** a connect briefly sets in
  `os.environ` during in-memory resolution (building a live source with `"x"`). It is an
  `RLock` because the config routes call `reload()` while already holding the lock.
  (`/config/validate` is read-only and does not take it.) Each connect also **updates
  `app._reloader` to match the new mode**: **local → a no-op reloader** (a config-watch
  reload must not swap the adopted bridge source out and orphan its runner), **remote /
  demo / mock → the normal `_select_source` reload**. `_select_source` itself is
  **marker-aware** — it re-selects through `_resolve_source_kind()` (the precedence), not
  bare `select_live()` — so a demo-mode reload stays mock instead of silently swapping to a
  resolvable remote config. The onboarding marker is the user's explicit *connection* choice
  and is changed **only by `/setup/connect`** (remote clears
  it, local/demo set it); the config-editor routes never touch it. So a user in local/demo
  mode who edits the config (even adding a server) **stays in that mode** until they
  explicitly re-onboard to remote — editing config *content* never silently switches the
  connection *mode*. (This is the deliberate resolution of the marker-vs-remote-config
  tension: an explicit choice sticks; the editor edits content, not mode.)

  **State coherence:** clearing the marker on remote success keeps the model
  single-valued — *remote == a usable `config.toml`, no opt-in marker*. So if that
  remote config later becomes unusable or is removed, precedence falls through to
  case 6 (`first_run`) and the welcome card resurfaces the problem, rather than a
  stale `demo`/`local` marker silently masking it.
- `{"choice":"demo"}` → transactional like local/remote: snapshot the prior marker,
  `_prepare_swap(MockSource())` (render — fallible) BEFORE `write_choice("demo")`, then
  `_commit_swap` (non-failing) + install the normal (mock) reloader. If prepare or the
  marker write fails, restore the prior marker, close the prepared source, and leave the
  previous source unchanged (`{"ok": false}`). On success → `{"ok": true}`.

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

**Timeout budget.** The whole remote transaction runs *inside* the sidecar's
`/setup/connect` handler: the probe (4 s) THEN build + render-prepare + keychain/config
snapshots + write + swap. So the `setup_connect` Rust proxy must use a **dedicated timeout
that covers the full worst case** — not just the probe — **≈ 15 s** (wide margin over the
4 s probe plus the sub-second post-probe work), **not** the shared 3 s `SIDECAR_TIMEOUT`.
Otherwise a slow-but-valid probe could make the Tauri command time out while the sidecar is
still mid-persist/swap — a torn result. `setup_status` keeps the default short timeout (no
network I/O).

### Rust proxy commands

Two token-injecting commands mirroring the existing config proxies, registered in
`invoke_handler!`:

- `setup_status()` → `GET /setup?token=…` → JSON (query-token, like `config_read`;
  default short `SIDECAR_TIMEOUT` — no network I/O).
- `setup_connect(body)` → `POST /setup/connect` (header token, like `config_validate`),
  returns the JSON `{ok, connected?, error?}`. Uses a **dedicated ≈15 s timeout**
  (covers the full remote transaction — probe + build + render + write + swap — not just
  the probe), not the shared 3 s `SIDECAR_TIMEOUT`. The token
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

### Re-onboarding beyond first run

The card must be reachable **after** first run, not only when `reason === "first_run"`.
Because a `demo`/`local` marker outranks a remote config and config edits never clear it,
a user who chose demo (or local while herdr is down) needs an explicit way back to the
connect flow. Provide a **"Change connection…"** entry point — a tray menu item and/or a
button in the config editor — that re-opens the `<Onboarding>` card (or invokes
`setup_connect` directly). The backend `/setup/connect` is **not** first-run-gated, so any
choice (incl. `remote`) can be re-selected at any time; this is purely the frontend
exposing it. Without it, a demo/local user would be pinned to their marker with no in-app
path to remote.

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
  `first_run`) **plus** a `local`/`demo` choice overriding a present remote config (the
  restart-selection guarantee); env/fs/choice injected as parameters (pure decision).
- `LocalBridgeRunner` — `start()` against `start_local_bridge(..., herdr=StubHerdr([…]))`:
  a real `Connector` (or `probe_server`) connects over the bound loopback port and
  receives the initial snapshot; `close()` tears the bridge down (no lingering task).
- onboarding-state module — `read_choice`/`write_choice` round-trip; absent file →
  `None`; atomic write; `HERDECK_CONFIG` redirection.
- `/setup` — JSON shape for each mode (mock/first_run/demo/local/remote).
- `/setup/connect` — `local` (starts bridge, swaps, persists), `remote` ok
  (probe ok → secret-then-config written + swaps + marker cleared), `remote` probe
  fail (nothing persisted, source unchanged), `remote` env-collision (`token_env`
  already exported with a different value → rejected up front, nothing persisted),
  `remote` token-env collision with another token_env user (another server `foo-bar` vs
  `foo_bar`, AND a `notifications.telegram` token_env → rejected, the other secret untouched),
  `remote` keychain READ failure
  (`peek_keychain` raises → abort BEFORE `set_secret`, an existing token is **not erased**),
  `remote` malformed/structurally-invalid existing config (unparseable, `servers = ["bad"]`,
  or an existing server dict missing `token_env` → `{ok:false}`/400, never a 500), `remote`
  non-string `id`/`url`/`token` (e.g. `{"id": 123}`) → 400, `remote` secret-store fail
  (`set_secret` raises → **no config written**, source unchanged, returns
  `{ok:false}`), `remote` selected-server mismatch (active profile / `overview_order`
  excludes the upserted server → `{ok:false}`, **nothing persisted** — verified
  before any write — no keychain entry, no config change), `remote` unloadable config
  (the selected server resolves but another selected server's token is missing → rejected,
  nothing persisted — the persisted config would not load on restart), `remote` live-clock
  adoption (`_commit_swap` adopts the `time.monotonic` clock passed to `_prepare_swap`, so
  a connect from the mock app gets live elapsed time, not the frozen mock clock), `remote`
  set_secret
  partial-overwrite-then-raises (→ `{ok:false}`, the PRIOR keychain value is restored),
  `remote` build failure
  (builder raises → `{ok:false}`, **nothing persisted** — build runs before persist —
  and the previous source AND a present local bridge are left **fully intact, no
  teardown** — asserted across a non-mock existing source), `remote` prepare/render failure
  (`_prepare_swap` raises after the source is built → the built source is **closed**,
  nothing persisted), `remote` config-snapshot read failure (aborts BEFORE `set_secret` →
  no orphaned secret, **built source closed**), `remote` config-write
  raises after the secret is set (→ `{ok:false}`, secret rolled back), `remote` partial
  write (config.toml written, local.toml faults → both files restored, secret rolled
  back, no leftover server), `remote` reconnect-existing-server write failure (the PRIOR
  keychain token + prior config are **restored, not destroyed/overwritten**), `remote`
  marker-clear failure (`clear_choice` faults → **full rollback** of config + secret,
  source unchanged, **and the built source closed** — no connector leak),
  reload-suppression (`DeckApp.reload` is a no-op while `_suppress_reload` is set) +
  `ConfigWatcher.resync` (adopts current mtimes as the baseline) + `swap_source`
  build-then-assign (a bad-config swap raises without half-swapping `self._source`),
  concurrent `/setup/connect` serialization (two parallel remote connects to the **same
  id** → serialized by `_setup_lock`, so the by-id upsert yields exactly one entry for
  that id, not a half-interleaved write; distinct ids / pre-existing servers are validly
  preserved — onboarding upserts by id, it does not replace the whole server list), a
  **`/config` write blocked by an in-flight connect** (the config write cannot proceed
  while a connect holds `_setup_lock`; the final config stays coherent — the shared-lock
  serialization across route types), local connect installs a **no-op reloader** (a later
  reload does not swap the bridge source out),
  `local` marker-write failure (build ok but `write_choice` faults →
  both the built source AND the bridge runner closed, previous source intact),
  `local` prepare failure (build ok but `_prepare_swap` faults before the marker →
  nothing persisted, built source + bridge runner closed, previous source intact;
  the post-marker commit cannot fail by construction), `local` bridge-start failure
  (`_start_local_bridge` raises → caught, not propagated, previous source intact),
  `remote` ok returns the **honest** `connected` (a source reporting `connected=False`
  is passed through, not hardcoded `true`), `demo` (transactional: prepare→marker→commit;
  marker-write failure → rollback, source unchanged), a `/config` write does **NOT** touch
  the marker (an explicit local/demo choice sticks across config edits); 400s for
  bad bodies.
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
- `src/herdeck/secrets.py` — `peek_keychain` (keychain-only read, for secret snapshot/restore).
- `src/herdeck/deckapp/config_service.py` — `resolve_selected_server` (in-memory resolve of the active profile's selected server).
- `src/herdeck/deckapp/watcher.py` — `ConfigWatcher.resync` (mtime-baseline resync for the watcher-suppressed commit).
- `src/herdeck/deckapp/server.py` — `select_source_kind`, local branch in
  `create_app`/`_select_source`, `DeckApp._set_local_bridge` + close wiring, the
  `connect` flow (build-before-persist, snapshot/restore of secret + config), the
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
