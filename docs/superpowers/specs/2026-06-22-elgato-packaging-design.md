# Elgato plugin packaging — frozen backend + `.streamDeckPlugin` (local, unsigned)

**Date:** 2026-06-22
**Status:** Approved design
**Scope:** Make the herdeck Elgato Stream Deck plugin installable on a Mac that has **no Python / no herdeck install**, by freezing the Python backend into a self-contained binary, bundling it inside the plugin, and producing an installable `.streamDeckPlugin` artifact.

## Goal

A user double-clicks `xyz.vaclavik.herdeck.streamDeckPlugin`, the Stream Deck app installs it, and the plugin works out of the box — the TypeScript shell spawns a **bundled frozen backend** instead of requiring `herdeck` on `PATH` or a configured Python venv.

Distribution target chosen by the user: **local self-contained bundle, unsigned.** No code signing, no notarization, no Apple Developer account, no Marketplace.

## Out of scope (explicit)

- Code signing / notarization (Developer ID) — the artifact is unsigned/ad-hoc; Gatekeeper may warn on other machines.
- universal2 / Intel (x86_64) — **arm64-only** for this milestone (matches the dev machine). universal2 needs a universal2 Python and is only worth it for distribution.
- Windows transport (Unix-socket-only stays).
- Marketplace submission + real (non-placeholder) icon art.
- On-hardware E2E is a manual gate, not automated here.

## Decisions (locked — these were technical forks the user delegated)

- **Freezer:** PyInstaller. Rejected: shiv/PEX (still need a system Python — defeats the goal), Nuitka (fragile with Pillow + lazy imports, slow builds), py2app (`.app`-oriented, overkill).
- **Freeze mode:** **onedir** (a `herdeck-backend/` folder = executable + dylibs + data). Faster cold start than onefile and **no per-launch self-extraction into a temp dir** — important because the shell spawns this as a child process and tmp self-extraction can trip Gatekeeper/AV. A folder inside the plugin bundle is acceptable.
- **Arch:** arm64-only.
- **No cairosvg at runtime** (see below) — the single biggest freeze risk, designed out.

## Architecture

Three coordinated pieces, same working tree:

### 1. Frozen-safe icon rendering (backend, Python)

The backend renders Stream Deck tiles via `src/herdeck/icons.py`, which uses **Pillow** (fine for PyInstaller) and **cairosvg** to rasterize the agent SVG glyphs (`_default_rasterize` → lazy `import cairosvg`). cairosvg pulls libcairo/pango (heavy, fragile native deps). We avoid bundling it entirely:

- **Build-time pre-rasterization:** rasterize `src/herdeck/assets/*.svg` → PNG (base glyphs at `ICON_SIZE`) and bundle the PNGs as plugin data.
- **Runtime injection:** `IconProvider.__init__` already accepts a `rasterize: Callable[[str, int], Image.Image]` seam, and `serve_elgato(...)` already accepts a `make_session` seam. When running **frozen** (`getattr(sys, "frozen", False)`), the session builder constructs `IconProvider` with a **PNG-loading rasterizer** that reads the pre-baked PNGs from the bundled assets dir (resolved via `sys._MEIPASS` / the executable dir) instead of `_default_rasterize`. Pillow-only at runtime.
- **PyInstaller `.spec`:** `excludes=['cairosvg', 'cffi', ...]` so static analysis of the lazy `import cairosvg` doesn't try (and fail) to bundle cairo. Ensure the label **TTF font** used by `ImageFont` is bundled as data.
- The non-frozen path (dev, tests) is unchanged — still uses cairosvg.

No new entry point: the frozen binary is `herdeck.app:main`, launched exactly as today (`HERDECK_ELGATO_SOCK` / `HERDECK_ELGATO_TOKEN` / `HERDECK_DECK=elgato-plugin`). Only the rasterizer + asset path differ when `sys.frozen`.

### 2. Bundled-binary discovery (TypeScript shell)

`resolveHerdeckCommand(opts)` currently resolves: **PI-configured path → `HERDECK_BIN` → `herdeck` on PATH**. Add the bundled binary as the zero-config default, keeping all overrides:

**PI path → `HERDECK_BIN` → bundled `backend/herdeck-backend` (if present) → `herdeck` on PATH**

- The bundled path is resolved **relative to the plugin directory**: `plugin.js` runs from `…sdPlugin/bin/`, so the binary is at `../backend/herdeck-backend` relative to the plugin.js location (derive from `import.meta.url`). Only used when the file actually exists (so dev checkouts without a built `backend/` fall through to PATH).
- Dev/venv workflows are unaffected: env (`HERDECK_BIN`) and the PI path still win.

### 3. Build pipeline (local)

A committed build script — `streamdeck/scripts/build-plugin.sh` (wired as an npm script, e.g. `npm run package`) — that runs:

1. **Pre-rasterize** SVG → PNG into the assets dir PyInstaller will bundle.
2. **PyInstaller** via a committed `.spec` → outputs the onedir folder into `streamdeck/xyz.vaclavik.herdeck.sdPlugin/backend/`.
3. **`npm run build`** (already exists) → `…sdPlugin/bin/plugin.js`.
4. **Package** `…sdPlugin/` → `xyz.vaclavik.herdeck.streamDeckPlugin` using Elgato's `DistributionTool` if on PATH, else a plain `zip` fallback (the `.streamDeckPlugin` format is a zip of the `.sdPlugin` dir).

Build outputs are **gitignored**: `streamdeck/.gitignore` already ignores `bin/` and `*.streamDeckPlugin`; add `backend/`. The pre-baked PNGs are build artifacts under the bundled assets dir — gitignored too (regenerated by the build).

## Testing

- **Python:** unit test that, when `sys.frozen` is simulated/forced, the session builder constructs `IconProvider` with the PNG rasterizer and never imports/calls cairosvg; a smoke test that the PNG rasterizer loads a pre-baked glyph and returns an `Image`. Keep the existing 403-test suite green.
- **TypeScript:** extend `resolveHerdeckCommand` unit tests — bundled-default precedence, existence check (missing `backend/` falls through to `herdeck`), and that PI path / `HERDECK_BIN` still win. Plugin-dir resolution from `import.meta.url`.
- **Build:** a lightweight check that `build-plugin.sh` produces the expected artifact paths (`backend/herdeck-backend`, `bin/plugin.js`, the `.streamDeckPlugin`). Full PyInstaller run is local/manual (not in CI for this milestone).
- **Manual gate:** install the `.streamDeckPlugin` on a Mac and confirm the backend launches with no Python present (overlaps the existing "on-hardware E2E" follow-up).

## Risks

- **Font availability when frozen** — `ImageFont` needs a TTF; bundle it explicitly as data, don't rely on a system font.
- **PyInstaller hidden imports** — Pillow plugins and any lazy imports in the elgato runtime; resolve in the `.spec` (`hiddenimports`) and verify the frozen binary actually serves (smoke run with the env contract).
- **Plugin-dir resolution** — must be robust to how the Stream Deck app launches Node (cwd is not the plugin dir); derive strictly from `import.meta.url`.
- **Bundle size** — Pillow + Python runtime is tens of MB; acceptable for a local bundle.
