# herdeck desktop (phase 1)

Tauri 2 (Rust) + Svelte + Vite floating shell for the herdeck deck. This is
**slice 3** of phase 1: the desktop **shell** only — a floating always-on-top
window + tray that spawns and supervises the Python sidecar
(`python -m herdeck.deckapp`), reads its discovery JSON, and hands the loopback
`url`+`token` to the WebView.

The real **DeckView** (poll `/state`, render `/tile` PNGs, click → `/press`) is
**slice 2** and replaces the placeholder marked in
`src/App.svelte` / `src/lib/DeckPlaceholder.svelte`.

The placeholder probes the sidecar's `/health` through the Rust `check_health`
command (loopback HTTP performed in Rust), **not** a browser `fetch`: the sidecar
is a different origin and sends no CORS headers, and this also keeps the access
token out of JS. Slice 2 must decide the same for `/state` + `/tile` (proxy via
Rust, or have the sidecar emit CORS headers).

## Layout

```
desktop/
  package.json, vite.config.ts, svelte.config.js, tsconfig.json
  index.html
  src/
    main.ts                  # Svelte 5 mount
    App.svelte               # PLACEHOLDER mount point for DeckView (slice 2)
    lib/
      sidecar.ts             # framework-free helpers (discovery + /health)
      sidecar.test.ts        # vitest unit tests
      DeckPlaceholder.svelte # small health-probe placeholder (slice 3 only)
  src-tauri/
    Cargo.toml, build.rs, tauri.conf.json
    capabilities/default.json
    icons/                   # generated PNG icons
    src/
      main.rs                # thin bin entry
      lib.rs                 # window + tray + supervisor wiring
      sidecar.rs             # spawn/parse/supervise logic (+ unit tests)
    tests/spawn.rs           # integration tests (real spawn, no Python needed)
```

## Develop

The Tauri CLI runs the Vite dev server and the Rust shell together. The shell, in
dev, resolves the repo `.venv` interpreter and runs `python -m herdeck.deckapp`,
so create that venv first:

```sh
# from the repo root: a venv with herdeck importable (+ the render deps)
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'   # pulls pillow etc. for the sidecar render

# then, from desktop/
npm install
npm run tauri dev                   # opens the floating window (real GUI)
```

`npm run tauri dev` is the exact manual command to smoke-test the GUI window. It
needs a desktop session (a real display); it will not render in a headless pane.

### Dev override (no venv / external sidecar)

To point the shell at a sidecar you started yourself (skips spawning):

```sh
HERDECK_DECKAPP_URL=http://127.0.0.1:PORT HERDECK_DECKAPP_TOKEN=THE_TOKEN \
  npm run tauri dev
```

## Test / build

```sh
npm install && npm test                 # vitest (frontend logic)

# the Rust shell embeds the built frontend via generate_context!, so build the
# frontend first, then build/test Rust:
npm run build                           # writes build/ (= frontendDist)
cd src-tauri && cargo build && cargo test
```

`npm run tauri build` / `npm run tauri dev` run `npm run build` automatically
(via `beforeBuildCommand` / `beforeDevCommand`); a *bare* `cargo build` needs
`build/` to already exist.
