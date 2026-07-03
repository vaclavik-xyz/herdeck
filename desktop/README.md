# herdeck desktop

Tauri 2 (Rust) + Svelte + Vite desktop app for herdeck: a floating,
always-on-top window that renders the live deck and a full settings UI.

The Rust shell owns the window + tray and either **attaches** to a herdeck
runtime that is already listening (via its discovery JSON) or **spawns and
supervises** a Python sidecar (`python -m herdeck.deckapp`). It reads the
sidecar's loopback `url` + access `token` and proxies `/state`, `/tile`, and
`/press` through Rust commands, so the token never crosses into JS (the sidecar
is a different origin and sends no CORS headers).

The Svelte frontend has two surfaces:

- **DeckView** — polls `/state`, renders the `/tile` PNGs, and turns clicks into
  `/press`, mirroring the hardware deck.
- **Onboarding + config editor** — a first-run onboarding flow and a sectioned
  settings editor (servers, theme, view, macros, notifications, safety,
  profiles) that reads and writes the herdeck config through the sidecar.

## Layout

```
desktop/
  package.json, vite.config.ts, svelte.config.js, tsconfig.json
  index.html
  src/
    main.ts                  # Svelte 5 mount
    App.svelte               # attaches/spawns sidecar; mounts Onboarding or DeckView
    lib/
      sidecar.ts             # framework-free discovery + /health helpers
      deckClient.ts          # /state + /tile + /press transport
      DeckView.svelte        # live deck render + press
      Onboarding.svelte      # first-run onboarding flow
      configClient.ts        # config read/write transport
      onboardingClient.ts    # onboarding/setup transport
      sections/              # config editor sections (servers, theme, view, …)
      fields/                # reusable form field components
      help.ts, i18n.svelte.ts, statusColors.ts, …
  src-tauri/
    Cargo.toml, build.rs, tauri.conf.json
    capabilities/default.json
    icons/                   # desktop app icons
    src/
      main.rs                # thin bin entry
      lib.rs                 # window + tray + sidecar supervisor + command wiring
      sidecar.rs             # spawn/parse/supervise logic (+ unit tests)
      http.rs                # loopback HTTP proxy with token injection (+ tests)
      window_mode.rs         # window mode (normal/floating/always-on-top)
      hotkey.rs              # global hotkey
    tests/                   # integration tests
```

## Develop

The Tauri CLI runs the Vite dev server and the Rust shell together. In dev the
shell resolves the repo `.venv` interpreter and runs `python -m herdeck.deckapp`,
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
