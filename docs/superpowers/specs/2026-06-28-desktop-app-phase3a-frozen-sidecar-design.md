# Desktop app — Phase 3a: frozen sidecar + installable macOS build

**Date:** 2026-06-28
**Status:** Approved design
**Phase:** 3a (first slice of Phase 3 "Distribuce & polish"; see
`2026-06-23-herdeck-desktop-app-overview.md`)

## Goal

A user double-clicks `herdeck.app` on an arm64 Mac that has **no Python and no
herdeck checkout**, and the app works: the floating deck + config editor run
against a **bundled frozen `herdeck.deckapp` sidecar** instead of a dev `.venv`
interpreter.

This slice is the keystone of Phase 3 — without a bundled sidecar there is no
installable artifact at all. It mirrors the already-shipped Elgato packaging
slice (`2026-06-22-elgato-packaging-design.md`): same PyInstaller onedir
strategy, same frozen-safe Pillow-PNG rendering, same "real freeze + headless
smoke" gate.

Distribution posture for this milestone: **arm64-only, unsigned / ad-hoc.**
Gatekeeper may warn on first launch; Developer ID signing + notarization is the
next slice (3b).

## Out of scope (explicit)

- **Signing / notarization** (Developer ID, stapling) — slice 3b. The artifact
  is unsigned; Gatekeeper warns on other machines.
- **universal2 / Intel (x86_64)** — arm64-only (matches the dev machine and the
  Elgato precedent). universal2 needs a universal2 Python and only matters for
  broad distribution.
- **Autostart, global hotkeys, real (non-placeholder) icons** — slice 3d.
- **Onboarding wizard** (first run with no config) — slice 3c.
- **Linux build** (AppImage/.deb + Linux-frozen sidecar) — slice 3e.
- **The GUI `tauri build` of the `.app` itself** is a manual gate the user runs
  on a Mac (it needs a desktop session + the GUI toolchain; it cannot run
  headless in an agent session). This slice wires + unit-tests the bundling and
  drives a real freeze + headless smoke of the **sidecar binary**.

## Current state (what blocks distribution today)

- The Rust shell spawns the sidecar **dev-mode only**: `resolve_dev_sidecar`
  (`desktop/src-tauri/src/sidecar.rs:75`) resolves `<repo>/.venv/bin/python -m
  herdeck.deckapp` with `PYTHONPATH=<repo>/src`. The frozen/bundled sidecar is
  explicitly deferred ("Frozen/bundled sidecar is a later phase").
- `desktop/src-tauri/tauri.conf.json`: `bundle.targets: "all"`, placeholder
  icons, **no `bundle.resources`**, no signing config.
- The deckapp sidecar renders tiles through `icons.py` (Pillow + cairosvg). A
  **frozen-safe** Pillow-only render path exists only for the Elgato backend
  (`elgato/frozen.py` + `elgato/runtime.py`); the deckapp has no frozen path yet.
- `deckapp/server.py:_default_icons()` builds `IconProvider(cache_dir, slug_map,
  fetch=lambda s: None)` — it does **not** pass the `rasterize` seam, so it falls
  back to `_default_rasterize` (cairosvg) when not given one.

## Architecture

Four coordinated pieces in the same working tree.

### 1. Frozen-safe rendering for the deckapp (Python)

The frozen helpers already written for Elgato are **generic** — they depend only
on `herdeck.icons.ICON_SIZE`, nothing elgato-specific:

- `is_frozen() -> bool`
- `baked_assets_dir() -> str` (resolves `sys._MEIPASS`/exe-dir + `herdeck_assets`)
- `glyph_png_name(svg_text: str) -> str` (content-addressed PNG filename)
- `make_png_rasterizer(baked_dir: str) -> Callable[[str, int], Image.Image]`
- `prerasterize_assets(src_dir, out_dir, size=BAKE_SIZE) -> list[str]` (build-time
  cairosvg baker)

**Move** these (and `BAKE_SIZE`) from `src/herdeck/elgato/frozen.py` to a neutral
`src/herdeck/frozen.py`. `src/herdeck/elgato/frozen.py` becomes a thin
**re-export** (`from ..frozen import *`-style explicit re-exports) so the Elgato
runtime keeps importing `from .frozen import ...` with zero behavior change. The
existing Elgato frozen tests must stay green.

**Wire the deckapp:** `deckapp/server.py:_default_icons()` — when `is_frozen()`,
construct `IconProvider(..., rasterize=make_png_rasterizer(baked_dir),
assets_dir=baked_dir)` where `baked_dir = baked_assets_dir()`. **Both** the
`rasterize` seam **and** `assets_dir` must point at the bundled dir — exactly like
the Elgato `_frozen_session` (`elgato/runtime.py:113-120`). `IconProvider`
defaults `assets_dir=_ASSETS_DIR` (`= src/herdeck/assets`,
`icons.py:32`), which does **not** exist in the frozen bundle; without the
override `_base_glyph` finds no SVG, the offline `fetch` returns `None`, and every
tile degrades to a letter glyph (the PNG rasterizer is never reached). The baked
PNGs live **in the same `herdeck_assets` dir** as the SVGs
(`prerasterize_assets` bakes in place), so one `assets_dir` serves both: the
provider reads `<agent>.svg` from it, and the rasterizer loads the content-keyed
PNG from it. When not frozen, behavior is unchanged (no `rasterize`/`assets_dir`
args → `_default_rasterize` + `_ASSETS_DIR` → cairosvg). The `fetch=lambda s:
None` offline default already holds in both paths. `IconProvider.__init__`
already exposes `rasterize: Callable[[str, int], Image.Image] = _default_rasterize`
and `assets_dir: str | None = _ASSETS_DIR` (`src/herdeck/icons.py:227-234`) — no
new seam needed.

### 2. Frozen sidecar bundle (PyInstaller)

A committed PyInstaller spec + thin entry, mirroring `streamdeck/herdeck-backend.spec`:

- **Entry:** `desktop/scripts/deckapp-entry.py` — `from herdeck.deckapp.__main__
  import main; sys.exit(main())`. (PyInstaller analyses a real script file, not a
  `-m` target.)
- **Spec:** `desktop/herdeck-deckapp.spec`:
  - `Analysis([deckapp-entry.py], pathex=[<repo>/src], ...)`.
  - `datas`: the baked PNG assets dir (`src/herdeck/assets` → `herdeck_assets`).
    No TTF is added to `datas` — matching the Elgato spec, which bundles none.
    `icons.py` does not load a bundled font: glyph tiles use the baked PNGs, and
    the **letter-fallback** path (`_font`, only hit for an agent with no SVG
    glyph) reads macOS system fonts (`_FONT_CANDIDATES`,
    e.g. `/System/Library/Fonts/Supplemental/Arial Bold.ttf`) with Pillow's
    bundled `ImageFont.load_default(size=...)` as the always-available safety net
    (PyInstaller's Pillow hook bundles that default font).
  - `excludes`: `cairosvg`, `cffi`, `cairocffi`, `tkinter`, `StreamDeck`, `hid`.
    The deckapp is a WS **bridge client** (it reaches herdr via `connector` +
    `websockets`); it never touches HID. Do **not** exclude `websockets`.
  - `hiddenimports`: deckapp submodules `herdeck.deckapp.server`,
    `herdeck.deckapp.live`, `herdeck.deckapp.mock`, `herdeck.deckapp.source`,
    `herdeck.deckapp.watcher`, `herdeck.deckapp.config_service`, plus
    `websockets` and `tomli_w` as safety nets (`tomli_w` is imported at the top of
    `config_service.py`; listing it guards against the lazy-import path being
    missed). Add more **only** if the real smoke run shows PyInstaller missed a
    reachable import.
  - `EXE(..., name="herdeck-deckapp", console=True, target_arch="arm64")`,
    `COLLECT(..., name="herdeck-deckapp")` → **onedir**. `COLLECT` itself creates a
    folder named `herdeck-deckapp/` **under** the `--distpath`, so the freeze runs
    with `--distpath desktop/src-tauri/resources` (**not**
    `…/resources/herdeck-deckapp`, which would double-nest). Result:
    `desktop/src-tauri/resources/herdeck-deckapp/herdeck-deckapp` (folder/exe) —
    the exact path the Rust resolver and the artifact check expect.
- **Onedir, not onefile:** matches the Elgato decision — no per-launch temp
  self-extraction (faster cold start, avoids Gatekeeper/AV friction from a child
  process extracting into tmp). A folder inside the `.app` Resources is fine.

The frozen binary is launched **exactly like the dev sidecar**: same argv-less
process emitting one discovery JSON line on stdout, same env contract
(`HERDECK_DECKAPP_PORT` optional). Only the program path differs.

### 3. Bundling + production resolution (Rust / Tauri)

- **`tauri.conf.json`:** add `bundle.resources` mapping the staged onedir folder
  into the app bundle so the executable lands at
  `<resource_dir>/herdeck-deckapp/herdeck-deckapp`. Use the **map form** to strip
  the `resources/` staging prefix:
  `"resources": { "resources/herdeck-deckapp": "herdeck-deckapp" }` (source is
  relative to `src-tauri/`; dest is relative to the bundle's resource dir). The
  plan confirms the exact Tauri placement against `app.path().resource_dir()` and
  adjusts the dest if Tauri nests differently — the binding constraint is that the
  staged path, the Tauri dest, and the Rust resolver path all agree on
  `<resource_dir>/herdeck-deckapp/herdeck-deckapp`. Narrow `bundle.targets` to
  macOS `["app", "dmg"]` for this arm64 milestone.
- **Rust resolution** (`sidecar.rs` + `lib.rs`): new precedence in `resolve_plan`:

  1. **env override** (`HERDECK_DECKAPP_URL` + `HERDECK_DECKAPP_TOKEN`) → trust an
     externally-started sidecar (unchanged dev path).
  2. **frozen bundled** → `<resource_dir>/herdeck-deckapp/herdeck-deckapp`, **only
     when that file exists** (so dev builds without a staged bundle fall through).
  3. **dev `.venv`** → `resolve_dev_sidecar` (unchanged fallback).

  New `resolve_frozen_sidecar(resource_dir: &Path) -> Option<CommandSpec>`: returns
  `Some(CommandSpec)` when the bundled binary exists (program = the binary,
  empty args, no special env), else `None`. Keep it **framework-free** (takes a
  `&Path`, not a Tauri handle) so it is unit-testable like `resolve_dev_sidecar`.
  `resolve_plan` gains a `resource_dir: Option<PathBuf>` parameter; `start_sidecar`
  passes `app.path().resource_dir().ok()`. In `tauri dev` the resource dir has no
  staged bundle → frozen check misses → `.venv` fallback. `CommandSpec`,
  `spawn_piped`, and `supervise` are untouched — the only change is *which* spec
  is chosen.

### 4. Build pipeline (committed scripts)

- **Build env:** the freeze runs in a Python env installed with the `packaging`
  extra — `pip install -e '.[packaging]'`. The existing `packaging` extra (used by
  the Elgato `build-plugin.sh`) already provides `pyinstaller>=6` + `cairosvg` +
  `pillow` + `websockets` + `keyring`. The **only** dep the deckapp frozen graph
  needs that `packaging` lacks is **`tomli-w`** (imported by
  `deckapp/config_service.py`); the deckapp graph does **not** reach
  `python-dotenv` or `deepdiff` (not imported anywhere in `src/herdeck`), and
  `keyring` is already in `packaging`. So this slice **adds `tomli-w` to the
  `packaging` extra** in `pyproject.toml` — making `packaging` the single
  reproducible freeze extra for both the Elgato backend and the desktop sidecar
  (`tomli-w` is harmless to the Elgato freeze). The full `deck` extra is **not**
  used for the freeze: it pulls `strmdck`/`hidapi`/`deepdiff` (HID/physical-deck
  deps, excluded from the bundle) and `strmdck` may not install cleanly in a fresh
  packaging env. The base `dependencies` (`websockets` only) is **not** enough —
  neither PyInstaller nor `tomli_w` is there.
- `desktop/scripts/build-sidecar.sh`:
  1. pre-rasterize `src/herdeck/assets/*.svg` → content-keyed PNGs into the assets
     dir PyInstaller bundles (idempotent; `prerasterize_assets`).
  2. PyInstaller `desktop/herdeck-deckapp.spec` with
     `--distpath desktop/src-tauri/resources` (COLLECT adds the `herdeck-deckapp/`
     folder) → `desktop/src-tauri/resources/herdeck-deckapp/herdeck-deckapp`.
- `desktop/scripts/build-app.sh`: run `build-sidecar.sh`, then `npm run tauri
  build` (which runs `npm run build` via `beforeBuildCommand`).
- **Gitignore** the staged bundle, the baked PNGs, and PyInstaller work dirs
  (build artifacts, regenerated by the build).

## Data flow (unchanged contract)

```
herdeck.app
  └─ Rust shell  resolve_plan(resource_dir)
       → Spawn( resources/herdeck-deckapp/herdeck-deckapp )   [prod]
       → Spawn( .venv/bin/python -m herdeck.deckapp )          [dev fallback]
            ↓ stdout: ONE discovery JSON line {url,host,port,token,source}
       → supervise() registers child, hands url+token to WebView (token Rust-side only)
            ↓
       frozen sidecar: create_app → _default_icons()
            is_frozen() → IconProvider(rasterize = PNG loader from herdeck_assets)
            render /tile, /panel, /state, /health  (Pillow-only, no cairosvg)
```

The discovery contract, token handling, and `/state`·`/tile`·`/press`·`/config`
proxy commands are all unchanged. This slice only changes **where the sidecar
comes from** and **how it rasterizes glyphs when frozen**.

## Testing

- **Python (TDD):**
  - `_default_icons()` uses the PNG rasterizer when `is_frozen()` is forced
    (monkeypatch), and the non-frozen path is unchanged (no `rasterize` override →
    cairosvg `_default_rasterize`). Assert the frozen path never imports/uses
    cairosvg.
  - Smoke: `make_png_rasterizer(<dir>)` loads a pre-baked glyph PNG and returns an
    `Image` of `ICON_SIZE`.
  - The moved `herdeck/frozen.py` keeps the existing Elgato frozen tests green via
    the re-export shim.
- **Rust (TDD):**
  - `resolve_frozen_sidecar` returns `Some(spec)` with the expected program path
    when the binary exists (use a `tempfile` layout) and `None` when it does not.
  - `resolve_plan` precedence: env override wins; otherwise frozen-if-exists wins
    over `.venv`; otherwise `.venv`. Existing `spawn`/`supervise` tests stay green.
- **Build-artifact check:** a lightweight assertion (in `build-sidecar.sh` or a
  smoke test) that the expected paths exist after a freeze:
  `resources/herdeck-deckapp/herdeck-deckapp` (executable) and the bundled
  `herdeck_assets/` with at least one baked PNG.
- **Real freeze + headless smoke gate (in-session, per chosen DoD):** run
  `build-sidecar.sh` to actually PyInstaller-freeze the deckapp, then spawn the
  frozen `herdeck-deckapp` binary headless and assert it (a) prints a valid
  discovery JSON line within the timeout, (b) serves a token-authed `GET /health`,
  (c) serves `GET /tile/0` returning a PNG (exercises the frozen PNG rasterizer +
  bundled `herdeck_assets`), and (d) serves `GET /config` (exercises the
  `config_service` import graph, i.e. that `tomli_w` is bundled). This catches real
  PyInstaller gaps (hidden imports, frozen rendering, missing assets, the
  `tomli_w` packaging dep) without a GUI.
- **Manual gate (user, on a Mac):** `npm run tauri build` → double-click
  `herdeck.app` → confirm the floating deck renders tiles and the config editor
  opens, with **no** Python / `.venv` present (overlaps the eventual signed-build
  E2E in 3b).

## Risks

- **Letter-fallback font when frozen** — the letter-fallback render path
  (`icons._font`, only for an agent with no SVG glyph) reads macOS system fonts
  and, failing those, Pillow's bundled `load_default(size=...)`. The mock smoke
  renders known agents (baked PNG glyphs), so it may not exercise the font path
  at all; the manual `.app` gate should eyeball a tile. No TTF is bundled (matches
  Elgato). If a future target lacks the system font, the bundled Pillow default
  covers it.
- **PyInstaller hidden imports + packaging deps** — the deckapp's reachable graph
  differs from the Elgato backend's (`live`/`mock`/`source`/`watcher`/
  `config_service` + `connector`/`websockets`), and `config_service` pulls
  `tomli_w` (not in `packaging` until this slice adds it). Resolve imports in the
  `.spec` `hiddenimports`, add `tomli-w` to the `packaging` extra, and verify the
  frozen binary actually serves `/health` + `/tile` + `/config` via the smoke run.
- **`resource_dir()` in dev vs bundle** — must fall through to `.venv` in `tauri
  dev` (no staged bundle). The `exists()` check on the bundled binary is the
  guard; cover it with the `resolve_plan` precedence test.
- **Bundle size** — Pillow + Python runtime is tens of MB; acceptable, matches the
  Elgato bundle.
- **Keychain access from a bundled (unsigned) app** — a live source reads the
  bridge token from the OS keychain; an unsigned app may prompt. Out of this
  slice's automated scope (mock works with no config); noted for the 3b signed
  E2E. The headless sidecar smoke uses the mock source (no config), so it is
  unaffected.
