# Elgato Plugin Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the herdeck Elgato plugin install and run on a Mac with no Python / no `herdeck` install, by freezing the backend (PyInstaller onedir), bundling it in the plugin, and producing an installable `.streamDeckPlugin`.

**Architecture:** Three coordinated pieces in the same tree. (1) A frozen-safe icon path in Python: when `sys.frozen`, the session builder injects a **PNG-loading rasterizer** (Pillow-only) through the existing `IconProvider(rasterize=…)` seam instead of cairosvg; PNGs are pre-baked at build time. (2) TS `resolveHerdeckCommand` gains a **bundled-binary default** (the frozen backend next to `plugin.js`), keeping every existing override. (3) A committed local build pipeline (`build-plugin.sh` + a PyInstaller `.spec`) pre-rasterizes, freezes, bundles, and zips.

**Tech Stack:** Python 3.12 + Pillow + (build-time only) cairosvg; PyInstaller (onedir, arm64); TypeScript + `@elgato/streamdeck` v2 + rollup + vitest; bash build script; Elgato `DistributionTool` (with `zip` fallback).

## Global Constraints

These bind every task. Exact values copied from `docs/superpowers/specs/2026-06-22-elgato-packaging-design.md`.

- **Freezer/mode:** PyInstaller, **onedir** (folder = exe + dylibs + data), **arm64-only**, macOS-only. No code signing / notarization (unsigned/ad-hoc artifact). No universal2/x86_64. No Windows.
- **No cairosvg at runtime:** the frozen backend must never import/call cairosvg. The PyInstaller `.spec` `excludes` cairosvg (+ its native chain `cffi`, `cairocffi`) so static analysis of the lazy `import cairosvg` does not try to bundle libcairo. cairosvg stays a **build-time-only** dependency (still used by the non-frozen dev/test path and by the pre-rasterizer).
- **Env contract unchanged:** the frozen binary is launched exactly as today — `HERDECK_ELGATO_SOCK`, `HERDECK_ELGATO_TOKEN`, `HERDECK_DECK=elgato-plugin`. No new entry point; the frozen entry is `herdeck.app:main`. Only the rasterizer + asset path differ when `sys.frozen`.
- **Offline-first when frozen:** no network glyph fetch in the frozen session (`fetch` returns `None`); only pre-baked PNGs + the letter fallback.
- **Icon geometry:** base glyphs are rasterized at `ICON_SIZE` (196). Pre-baked PNG filenames are keyed by the **sha1 of the SVG text** (so the build-time baker and the runtime loader agree on the key without touching `IconProvider`'s `rasterize(svg, size)` seam).
- **Discovery precedence (TS):** **PI-configured path → `HERDECK_BIN` → bundled `backend/herdeck-backend/herdeck-backend` (only if the file exists) → `herdeck` on PATH.** Overrides always win; the bundled default is used only when present, so dev checkouts without a built `backend/` fall through to PATH.
- **Bundled path resolution:** derive strictly from `import.meta.url` (the Stream Deck app's cwd is not the plugin dir).
- **Build artifacts are gitignored:** `streamdeck/backend/` → already covered by adding `backend/` to `streamdeck/.gitignore`; pre-baked `src/herdeck/assets/*.png` and the PyInstaller `build/` workdir → root `.gitignore`.
- **Keep the existing 403-test Python suite green** and the TS suite green. Run Python tests with `.venv/bin/python -m pytest`; TS tests with `npm test` in `streamdeck/`.
- Commits: conventional-commit format, **no `Co-Authored-By`**.

### ⚑ Orchestrator redline decisions (RESOLVED)

1. **onedir exe is one level deeper than the spec prose — ACCEPTED as written.** PyInstaller onedir produces `<distpath>/<name>/<name>` (folder named after the app, exe inside). With `--distpath …sdPlugin/backend` and `name=herdeck-backend` the exe lands at `…sdPlugin/backend/herdeck-backend/herdeck-backend`. TS resolves the bundled path to this real layout (`../backend/herdeck-backend/herdeck-backend`). The spec's `backend/` gitignore is preserved.
2. **Font bundling — CUT.** Task 4 is dropped. `_FONT_CANDIDATES` in `icons.py` already lists macOS system fonts (`/System/Library/Fonts/Supplemental/Arial Bold.ttf`, `HelveticaNeue.ttc`) which are present even in a frozen run (PyInstaller does not sandbox the filesystem), plus the `ImageFont.load_default` fallback. On the target local arm64 Mac, labels + letter glyphs render without a bundled TTF. No `add_font_search_path`, no DejaVu TTF, no `pyproject` package-data change, no font-related change to `_frozen_session`.
3. **The "build" test is a static gate, not a PyInstaller run — ACCEPTED.** Per the spec ("Full PyInstaller run is local/manual (not in CI)"), Tasks 6–7 assert the `.spec`/script *content and wiring* (excludes, datas, arch, step order, output paths). The actual freeze + on-device install is the **manual gate** in Task 8.

**Task order:** 1, 2, 3, ~~4~~ (CUT), 5, 6, 7, 8.

---

## File Structure

**Created:**
- `src/herdeck/elgato/frozen.py` — frozen detection, bundled-asset dir resolution, sha1 PNG key, PNG-loading rasterizer factory, and the build-time pre-rasterizer. Shared by runtime (loader) and build (baker) so the key never drifts.
- `streamdeck/herdeck-backend.spec` — committed PyInstaller spec (onedir, arm64, excludes cairosvg, bundles `assets`).
- `streamdeck/scripts/herdeck-backend-entry.py` — tiny freeze entry that calls `herdeck.app.main`.
- `streamdeck/scripts/build-plugin.sh` — the local build pipeline (pre-rasterize → freeze → npm build → package).
- `tests/test_elgato_frozen.py` — Python unit tests for `frozen.py`.
- `streamdeck/tests/bundled-discovery.test.ts` — TS tests for bundled-binary discovery.
- `streamdeck/tests/packaging.test.ts` — static gate over the `.spec` + build script.

**Modified:**
- `src/herdeck/elgato/runtime.py` — add `_frozen_session` + a `_session_for_runtime` dispatcher; make it `serve_elgato`'s default `make_session`.
- `streamdeck/src/backend-process.ts` — bundled-binary precedence + `bundledBackendPath(importMetaUrl)`.
- `streamdeck/src/plugin.ts` — pass the resolved bundled path into `resolveHerdeckCommand`.
- `streamdeck/package.json` — add `"package"` script.
- `streamdeck/.gitignore` — add `backend/`.
- `.gitignore` (root) — add `src/herdeck/assets/*.png` and `build/`.
- `README.md` / packaging docs — how to build + install the `.streamDeckPlugin` (Task 8).

---

## Task 1: Frozen runtime core (`frozen.py` — loader side)

**Files:**
- Create: `src/herdeck/elgato/frozen.py`
- Test: `tests/test_elgato_frozen.py`

**Interfaces:**
- Produces:
  - `BAKE_SIZE: int` (= `ICON_SIZE`, 196)
  - `is_frozen() -> bool`
  - `baked_assets_dir() -> str` — the bundled assets dir at runtime (`<sys._MEIPASS>/herdeck_assets`)
  - `glyph_png_name(svg_text: str) -> str` — `"<sha1(svg_text)>.png"`
  - `make_png_rasterizer(baked_dir: str) -> Callable[[str, int], Image.Image]` — loads `baked_dir/<glyph_png_name(svg)>`, returns an RGBA `Image` resized to `size`
- Consumes: `herdeck.icons.ICON_SIZE`, Pillow `Image`. **No cairosvg.**

- [ ] **Step 1: Write the failing test**

```python
# tests/test_elgato_frozen.py
import sys

from PIL import Image

from herdeck.elgato import frozen
from herdeck.icons import ICON_SIZE


def test_bake_size_matches_icon_size():
    assert frozen.BAKE_SIZE == ICON_SIZE


def test_glyph_png_name_is_stable_and_content_keyed():
    a = frozen.glyph_png_name("<svg>codex</svg>")
    assert a == frozen.glyph_png_name("<svg>codex</svg>")  # deterministic
    assert a != frozen.glyph_png_name("<svg>other</svg>")  # content-keyed
    assert a.endswith(".png") and "/" not in a


def test_is_frozen_reflects_sys_frozen(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert frozen.is_frozen() is False
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert frozen.is_frozen() is True


def test_png_rasterizer_loads_prebaked_glyph(tmp_path):
    svg = "<svg>codex</svg>"
    baked = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (10, 20, 30, 255))
    baked.save(tmp_path / frozen.glyph_png_name(svg))
    rasterize = frozen.make_png_rasterizer(str(tmp_path))
    img = rasterize(svg, ICON_SIZE)
    assert isinstance(img, Image.Image)
    assert img.mode == "RGBA" and img.size == (ICON_SIZE, ICON_SIZE)
    assert img.getpixel((0, 0)) == (10, 20, 30, 255)


def test_png_rasterizer_resizes_to_requested_size(tmp_path):
    svg = "<svg>x</svg>"
    Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (1, 2, 3, 255)).save(
        tmp_path / frozen.glyph_png_name(svg)
    )
    img = frozen.make_png_rasterizer(str(tmp_path))(svg, 64)
    assert img.size == (64, 64)


def test_png_rasterizer_never_imports_cairosvg(tmp_path, monkeypatch):
    import builtins

    svg = "<svg>x</svg>"
    Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 255)).save(
        tmp_path / frozen.glyph_png_name(svg)
    )
    real_import = builtins.__import__

    def guard(name, *a, **k):
        assert name != "cairosvg", "frozen rasterizer must not import cairosvg"
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", guard)
    frozen.make_png_rasterizer(str(tmp_path))(svg, ICON_SIZE)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_elgato_frozen.py -v`
Expected: FAIL — `ModuleNotFoundError: herdeck.elgato.frozen`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/herdeck/elgato/frozen.py
from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Callable

from PIL import Image

from ..icons import ICON_SIZE

BAKE_SIZE = ICON_SIZE


def is_frozen() -> bool:
    """True when running inside a PyInstaller (or similar) frozen bundle."""
    return bool(getattr(sys, "frozen", False))


def baked_assets_dir() -> str:
    """The bundled assets dir at runtime.

    PyInstaller sets ``sys._MEIPASS`` in both onefile and onedir modes; the
    ``.spec`` bundles ``src/herdeck/assets`` as data under ``herdeck_assets``.
    """
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(sys.executable))
    return os.path.join(base, "herdeck_assets")


def glyph_png_name(svg_text: str) -> str:
    """Content-addressed PNG filename for an SVG glyph.

    The build-time baker and the runtime loader both key on this, so neither
    needs to know the agent type — keeping ``IconProvider``'s ``rasterize(svg,
    size)`` seam untouched.
    """
    return hashlib.sha1(svg_text.encode("utf-8")).hexdigest() + ".png"


def make_png_rasterizer(baked_dir: str) -> Callable[[str, int], Image.Image]:
    """A Pillow-only rasterizer that returns a pre-baked PNG for an SVG glyph."""

    def rasterize(svg: str, size: int) -> Image.Image:
        path = os.path.join(baked_dir, glyph_png_name(svg))
        img = Image.open(path).convert("RGBA")
        if img.size != (size, size):
            img = img.resize((size, size))
        return img

    return rasterize
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_elgato_frozen.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/elgato/frozen.py tests/test_elgato_frozen.py
git commit -m "feat: add frozen-runtime icon loader (sha1-keyed PNG rasterizer)"
```

---

## Task 2: Build-time pre-rasterizer (`frozen.py` — baker side)

**Files:**
- Modify: `src/herdeck/elgato/frozen.py`
- Test: `tests/test_elgato_frozen.py`

**Interfaces:**
- Produces: `prerasterize_assets(src_dir: str, out_dir: str, size: int = BAKE_SIZE) -> list[str]` — rasterizes every `*.svg` in `src_dir` to `out_dir/<glyph_png_name(svg_text)>` via cairosvg; returns the written filenames. Lazy `import cairosvg` (build-time only).
- Consumes: `glyph_png_name`, cairosvg (build env only — present in the `dev` extra).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_elgato_frozen.py
import pytest


def test_prerasterize_writes_content_keyed_pngs(tmp_path):
    pytest.importorskip("cairosvg")  # build-time dep; present in the dev extra
    from PIL import Image

    src = tmp_path / "assets"
    src.mkdir()
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">' \
          '<rect width="10" height="10" fill="#fff"/></svg>'
    (src / "codex.svg").write_text(svg, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()

    written = frozen.prerasterize_assets(str(src), str(out), frozen.BAKE_SIZE)

    expected = frozen.glyph_png_name(svg)
    assert written == [expected]
    baked = out / expected
    assert baked.exists()
    with Image.open(baked) as im:
        assert im.size == (frozen.BAKE_SIZE, frozen.BAKE_SIZE)
    # The runtime loader round-trips against what the baker wrote.
    assert frozen.make_png_rasterizer(str(out))(svg, frozen.BAKE_SIZE).size == (
        frozen.BAKE_SIZE,
        frozen.BAKE_SIZE,
    )


def test_prerasterize_into_same_dir_is_idempotent(tmp_path):
    pytest.importorskip("cairosvg")
    src = tmp_path / "assets"
    src.mkdir()
    (src / "x.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    first = frozen.prerasterize_assets(str(src), str(src), frozen.BAKE_SIZE)
    second = frozen.prerasterize_assets(str(src), str(src), frozen.BAKE_SIZE)
    assert first == second  # re-running over the same dir is stable
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_elgato_frozen.py::test_prerasterize_writes_content_keyed_pngs -v`
Expected: FAIL — `AttributeError: module 'herdeck.elgato.frozen' has no attribute 'prerasterize_assets'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/herdeck/elgato/frozen.py
import io


def prerasterize_assets(src_dir: str, out_dir: str, size: int = BAKE_SIZE) -> list[str]:
    """Build-time: rasterize each ``*.svg`` in ``src_dir`` to a content-keyed PNG.

    Uses cairosvg (build-time only; excluded from the frozen bundle). Skips any
    ``*.png`` already in ``src_dir`` so baking into the source assets dir is safe.
    """
    import cairosvg  # build-time only — never bundled (see the .spec excludes)

    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []
    for entry in sorted(os.listdir(src_dir)):
        if not entry.endswith(".svg"):
            continue
        with open(os.path.join(src_dir, entry), encoding="utf-8") as fh:
            svg = fh.read()
        png = cairosvg.svg2png(bytestring=svg.encode(), output_width=size, output_height=size)
        Image.open(io.BytesIO(png)).convert("RGBA").save(os.path.join(out_dir, glyph_png_name(svg)))
        written.append(glyph_png_name(svg))
    return written
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_elgato_frozen.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/elgato/frozen.py tests/test_elgato_frozen.py
git commit -m "feat: add build-time SVG->PNG pre-rasterizer for frozen assets"
```

---

## Task 3: Frozen-aware session builder in the runtime

**Files:**
- Modify: `src/herdeck/elgato/runtime.py`
- Test: `tests/test_elgato_runtime.py`

**Interfaces:**
- Consumes: `frozen.is_frozen`, `frozen.baked_assets_dir`, `frozen.make_png_rasterizer`, existing `_default_session`, `IconProvider`, `DEFAULT_AGENT_SLUGS`, `ElgatoSession`.
- Produces:
  - `_frozen_session(config: Config, baked_dir: str) -> ElgatoSession` — builds `IconProvider` with `rasterize=make_png_rasterizer(baked_dir)`, `assets_dir=baked_dir`, `fetch=lambda slug: None` (offline).
  - `_session_for_runtime(config: Config) -> ElgatoSession` — frozen → `_frozen_session(config, baked_assets_dir())`; else → `_default_session(config)`.
  - `serve_elgato(..., make_session=_session_for_runtime)` (default changed from `_default_session`).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_elgato_runtime.py
def _copy_assets_and_bake(tmp_path):
    """Stage a baked assets dir (svg + content-keyed png) like the build does."""
    import pytest

    pytest.importorskip("cairosvg")
    import shutil

    from herdeck.elgato import frozen

    src = "src/herdeck/assets"
    staged = tmp_path / "herdeck_assets"
    staged.mkdir()
    for name in os.listdir(src):
        if name.endswith(".svg"):
            shutil.copy(os.path.join(src, name), staged / name)
    frozen.prerasterize_assets(str(staged), str(staged), frozen.BAKE_SIZE)
    return str(staged)


def test_frozen_session_uses_png_rasterizer(tmp_path):
    import os as _os

    from herdeck.elgato import frozen
    from herdeck.elgato.runtime import _frozen_session

    baked = _copy_assets_and_bake(tmp_path)
    sess = _frozen_session(_cfg(), baked)
    icons = sess._icons
    # PNG-loading rasterizer + bundled assets dir + offline fetch.
    assert icons._assets_dir == baked
    assert icons._fetch("claude") is None  # no network when frozen
    # The bundled-asset agent (codex.svg -> baked PNG) renders without cairosvg.
    name = icons.icon_for("codex", "green")
    assert _os.path.exists(_os.path.join(icons._cache_dir, name))


def test_session_for_runtime_dispatches_on_frozen(monkeypatch, tmp_path):
    import sys

    from herdeck.elgato import runtime

    baked = _copy_assets_and_bake(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime, "_baked_assets_dir", lambda: baked)
    sess = runtime._session_for_runtime(_cfg())
    assert sess._icons._assets_dir == baked  # frozen branch taken


def test_session_for_runtime_uses_default_when_not_frozen(monkeypatch):
    import sys

    from herdeck.elgato import runtime
    from herdeck.icons import _ASSETS_DIR, _default_fetch

    monkeypatch.delattr(sys, "frozen", raising=False)
    sess = runtime._session_for_runtime(_cfg())
    # Dev path unchanged: package assets dir + real network fetch.
    assert sess._icons._assets_dir == _ASSETS_DIR
    assert sess._icons._fetch is _default_fetch


def test_serve_elgato_default_make_session_is_runtime_dispatcher():
    import inspect

    from herdeck.elgato.runtime import _session_for_runtime, serve_elgato

    assert inspect.signature(serve_elgato).parameters["make_session"].default is _session_for_runtime
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_elgato_runtime.py -k "frozen or runtime_dispatch or runtime_default or runtime_uses_default" -v`
Expected: FAIL — `ImportError: cannot import name '_frozen_session'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/herdeck/elgato/runtime.py
# add near the other imports at module top:
from .frozen import baked_assets_dir as _baked_assets_dir
from .frozen import is_frozen as _is_frozen
from .frozen import make_png_rasterizer as _make_png_rasterizer

# add after _default_session(...):
def _frozen_session(config: Config, baked_dir: str) -> ElgatoSession:
    """Session for a frozen bundle: Pillow-only PNG rasterizer, bundled assets,
    no network glyph fetch."""
    import tempfile

    cache = os.path.join(tempfile.gettempdir(), "herdeck-elgato-icons")
    overrides = (
        os.path.abspath(os.path.expanduser(config.hardware.icons_dir))
        if config.hardware.icons_dir
        else None
    )
    icons = IconProvider(
        cache_dir=cache,
        slug_map=DEFAULT_AGENT_SLUGS,
        overrides_dir=overrides,
        fetch=lambda slug: None,  # offline-first: no Simple Icons fetch when frozen
        rasterize=_make_png_rasterizer(baked_dir),
        assets_dir=baked_dir,
    )
    return ElgatoSession(config, icons)


def _session_for_runtime(config: Config) -> ElgatoSession:
    """Pick the session builder by runtime: frozen bundle vs dev/test."""
    if _is_frozen():
        return _frozen_session(config, _baked_assets_dir())
    return _default_session(config)
```

Then change the `serve_elgato` signature default:

```python
async def serve_elgato(config: Config, *, socket_path: str, token: str, make_session=_session_for_runtime) -> None:
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_elgato_runtime.py -v`
Expected: PASS (all runtime tests, including the new four).

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/elgato/runtime.py tests/test_elgato_runtime.py
git commit -m "feat: pick frozen vs dev session builder by sys.frozen"
```

---

## Task 4: ~~Bundle a TTF font for the frozen letter glyph~~ — **CUT**

**Orchestrator decision (redline 2): dropped.** `_FONT_CANDIDATES` in `icons.py` already
lists macOS system fonts (`/System/Library/Fonts/Supplemental/Arial Bold.ttf`,
`HelveticaNeue.ttc`), which remain readable in a frozen run (PyInstaller does not sandbox
the filesystem), and there is an `ImageFont.load_default` fallback. On the target local
arm64 Mac, panel labels + letter glyphs render without a bundled TTF, so no font is
bundled: no `add_font_search_path`, no DejaVu TTF, no `pyproject` package-data change, no
font-related change to `_frozen_session` (it stays exactly as built in Task 3).

---

## Task 5: Bundled-binary discovery (TypeScript)

**Files:**
- Modify: `streamdeck/src/backend-process.ts`, `streamdeck/src/plugin.ts`
- Test: `streamdeck/tests/bundled-discovery.test.ts`, `streamdeck/tests/backend-process.test.ts`

**Interfaces:**
- Produces:
  - `resolveHerdeckCommand(opts: { configuredPath?: string; envBin?: string; bundledPath?: string; exists?: (p: string) => boolean }): { command: string; args: string[] }` — precedence: `configuredPath` → `envBin` → `bundledPath` (only if `exists(bundledPath)`) → `"herdeck"`. `exists` defaults to `fs.existsSync`.
  - `bundledBackendPath(importMetaUrl: string): string` — resolves `<binDir>/../backend/herdeck-backend/herdeck-backend` from `plugin.js`'s URL.
- Consumes (plugin.ts): `bundledBackendPath(import.meta.url)`.

- [ ] **Step 1: Write the failing test**

```typescript
// streamdeck/tests/bundled-discovery.test.ts
import { describe, it, expect } from "vitest";
import { resolveHerdeckCommand, bundledBackendPath } from "../src/backend-process.js";

const yes = () => true;
const no = () => false;

describe("resolveHerdeckCommand bundled precedence", () => {
  it("PI path and HERDECK_BIN win over a present bundled binary", () => {
    expect(resolveHerdeckCommand({ configuredPath: "/opt/h", bundledPath: "/b", exists: yes }))
      .toEqual({ command: "/opt/h", args: [] });
    expect(resolveHerdeckCommand({ envBin: "/usr/bin/herdeck", bundledPath: "/b", exists: yes }))
      .toEqual({ command: "/usr/bin/herdeck", args: [] });
  });

  it("uses the bundled binary when present and no override is set", () => {
    expect(resolveHerdeckCommand({ bundledPath: "/plugin/backend/herdeck-backend/herdeck-backend", exists: yes }))
      .toEqual({ command: "/plugin/backend/herdeck-backend/herdeck-backend", args: [] });
  });

  it("falls through to PATH when the bundled binary is absent (dev checkout)", () => {
    expect(resolveHerdeckCommand({ bundledPath: "/plugin/backend/herdeck-backend/herdeck-backend", exists: no }))
      .toEqual({ command: "herdeck", args: [] });
  });

  it("with nothing set, resolves to herdeck on PATH", () => {
    expect(resolveHerdeckCommand({ exists: no })).toEqual({ command: "herdeck", args: [] });
  });
});

describe("bundledBackendPath", () => {
  it("derives ../backend/herdeck-backend/herdeck-backend from plugin.js URL", () => {
    const url = "file:///Users/x/xyz.vaclavik.herdeck.sdPlugin/bin/plugin.js";
    expect(bundledBackendPath(url)).toBe(
      "/Users/x/xyz.vaclavik.herdeck.sdPlugin/backend/herdeck-backend/herdeck-backend",
    );
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd streamdeck && npx vitest run tests/bundled-discovery.test.ts`
Expected: FAIL — `bundledBackendPath` is not exported; bundled precedence not honored.

- [ ] **Step 3: Write minimal implementation**

```typescript
// streamdeck/src/backend-process.ts — replace the resolveHerdeckCommand block, add imports
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";

export function resolveHerdeckCommand(opts: {
  configuredPath?: string;
  envBin?: string;
  bundledPath?: string;
  exists?: (p: string) => boolean;
}): { command: string; args: string[] } {
  // PI-configured path wins (the Stream Deck app's PATH usually lacks the user's venv),
  // then HERDECK_BIN, then the bundled frozen backend (only if actually present so dev
  // checkouts fall through), then `herdeck` on PATH.
  const exists = opts.exists ?? existsSync;
  const command =
    opts.configuredPath ||
    opts.envBin ||
    (opts.bundledPath && exists(opts.bundledPath) ? opts.bundledPath : undefined) ||
    "herdeck";
  return { command, args: [] };
}

export function bundledBackendPath(importMetaUrl: string): string {
  // plugin.js runs from …sdPlugin/bin/; the PyInstaller onedir folder sits at
  // …sdPlugin/backend/herdeck-backend/ with the executable inside it.
  const binDir = path.dirname(fileURLToPath(importMetaUrl));
  return path.join(binDir, "..", "backend", "herdeck-backend", "herdeck-backend");
}
```

Wire it in `plugin.ts` (inside the `connect().then(...)` body, where `resolveCommand` is built):

```typescript
  import { BackendProcess, resolveHerdeckCommand, bundledBackendPath } from "./backend-process.js";
  // ...
  const bundled = bundledBackendPath(import.meta.url);
  const backend = new BackendProcess({
    resolveCommand: () =>
      resolveHerdeckCommand({
        configuredPath: herdeckPath,
        envBin: process.env.HERDECK_BIN,
        bundledPath: bundled,
      }),
    devSocket: process.env.HERDECK_ELGATO_DEV_SOCK,
    devToken: process.env.HERDECK_ELGATO_TOKEN,
  });
```

(The existing `backend-process.test.ts` precedence test stays valid — no `bundledPath` → `herdeck`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd streamdeck && npm test`
Expected: PASS (new file + the full suite). Then `npx tsc --noEmit` clean.

- [ ] **Step 5: Commit**

```bash
git add streamdeck/src/backend-process.ts streamdeck/src/plugin.ts streamdeck/tests/bundled-discovery.test.ts
git commit -m "feat: discover the bundled frozen backend in resolveHerdeckCommand"
```

---

## Task 6: PyInstaller spec + freeze entry script

**Files:**
- Create: `streamdeck/herdeck-backend.spec`, `streamdeck/scripts/herdeck-backend-entry.py`
- Test: `streamdeck/tests/packaging.test.ts`

**Interfaces:**
- Produces: a committed onedir arm64 `.spec` that excludes cairosvg, bundles `src/herdeck/assets` → `herdeck_assets`, and an entry script that runs `herdeck.app.main`.
- The static gate (`packaging.test.ts`) asserts the `.spec`/entry content; the real freeze is the manual gate (Task 8).

- [ ] **Step 1: Write the failing test**

```typescript
// streamdeck/tests/packaging.test.ts
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");
const spec = read("../herdeck-backend.spec");
const entry = read("../scripts/herdeck-backend-entry.py");

describe("PyInstaller spec", () => {
  it("freezes onedir, arm64, named herdeck-backend", () => {
    expect(spec).toMatch(/name\s*=\s*['"]herdeck-backend['"]/);
    expect(spec).toMatch(/target_arch\s*=\s*['"]arm64['"]/);
    expect(spec).toContain("COLLECT("); // onedir (COLLECT), not a onefile EXE-only build
  });

  it("excludes cairosvg + native chain AND the lazy driver stack the elgato path never hits", () => {
    const excludes = spec.slice(spec.indexOf("excludes"), spec.indexOf("excludes") + 200);
    for (const mod of ["cairosvg", "cffi", "cairocffi", "StreamDeck", "hid", "serial"]) {
      expect(excludes).toContain(mod);
    }
    // websockets belongs to the (lazy, unreached) web driver — it must NOT be a hidden import.
    expect(spec).not.toMatch(/hiddenimports\s*=\s*\[[^\]]*websockets/s);
  });

  it("bundles the assets dir as herdeck_assets data", () => {
    expect(spec).toContain("herdeck_assets");
    expect(spec).toMatch(/assets/);
  });
});

describe("freeze entry script", () => {
  it("invokes herdeck.app.main", () => {
    expect(entry).toContain("from herdeck.app import main");
    expect(entry).toMatch(/main\(\)/);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd streamdeck && npx vitest run tests/packaging.test.ts`
Expected: FAIL — the `.spec` and entry files do not exist (`ENOENT`).

- [ ] **Step 3: Write minimal implementation**

```python
# streamdeck/scripts/herdeck-backend-entry.py
"""PyInstaller entry: launch the herdeck backend exactly like the console script."""
from herdeck.app import main

if __name__ == "__main__":
    main()
```

```python
# streamdeck/herdeck-backend.spec  — PyInstaller onedir, arm64, no cairosvg
# Build with: pyinstaller streamdeck/herdeck-backend.spec --distpath …/backend --workpath build/pyi
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # repo root (SPECPATH = streamdeck/)

a = Analysis(
    [os.path.join(SPECPATH, "scripts", "herdeck-backend-entry.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=[],
    datas=[(os.path.join(ROOT, "src", "herdeck", "assets"), "herdeck_assets")],
    # Only the elgato submodules serve_elgato actually reaches. (No "websockets": that is a
    # web-driver dep, and the web/d200/elgato-hw drivers are imported lazily inside make_deck,
    # which the elgato path — _amain_elgato — returns before ever calling. Add to this list
    # ONLY if the manual smoke run shows PyInstaller missed a real serve_elgato-graph import.)
    hiddenimports=[
        "herdeck.elgato.runtime",
        "herdeck.elgato.frozen",
        "herdeck.elgato.session",
        "herdeck.elgato.ipc",
    ],
    # cairosvg (+ its native cffi/cairocffi chain) is build-time only — the frozen session
    # uses the Pillow PNG rasterizer. Also exclude the native driver stack that only the
    # lazy, unreached make_deck importers pull (StreamDeck/hidapi, pyserial → `serial`,
    # web-driver `websockets`) so the bundle stays slim — no libusb/hidapi/pyserial.
    # NEVER exclude anything in the serve_elgato import graph.
    excludes=["cairosvg", "cffi", "cairocffi", "tkinter", "StreamDeck", "hid", "serial", "websockets"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="herdeck-backend",
    console=True,
    target_arch="arm64",
)
coll = COLLECT(exe, a.binaries, a.datas, name="herdeck-backend")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd streamdeck && npx vitest run tests/packaging.test.ts`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add streamdeck/herdeck-backend.spec streamdeck/scripts/herdeck-backend-entry.py streamdeck/tests/packaging.test.ts
git commit -m "feat: add PyInstaller onedir spec + freeze entry for the backend"
```

---

## Task 7: Build pipeline script + gitignore wiring

**Files:**
- Create: `streamdeck/scripts/build-plugin.sh`
- Modify: `streamdeck/package.json`, `streamdeck/.gitignore`, `.gitignore` (root)
- Test: `streamdeck/tests/packaging.test.ts`

**Interfaces:**
- Produces: an executable `build-plugin.sh` that runs the four steps and an npm `package` script that calls it. Output artifact: `streamdeck/xyz.vaclavik.herdeck.streamDeckPlugin`.

- [ ] **Step 1: Write the failing test**

```typescript
// append to streamdeck/tests/packaging.test.ts
const build = read("../scripts/build-plugin.sh");
const pkg = JSON.parse(read("../package.json"));
const sdIgnore = read("../.gitignore");

describe("build-plugin.sh pipeline", () => {
  it("runs the four steps in order: pre-rasterize, freeze, npm build, package", () => {
    const i = (s: string) => build.indexOf(s);
    expect(i("prerasterize_assets")).toBeGreaterThan(-1);          // 1. pre-rasterize
    expect(i("herdeck-backend.spec")).toBeGreaterThan(i("prerasterize_assets")); // 2. freeze
    expect(i("npm run build")).toBeGreaterThan(i("herdeck-backend.spec"));        // 3. TS build
    expect(i(".streamDeckPlugin")).toBeGreaterThan(i("npm run build"));           // 4. package
  });

  it("freezes into the plugin's backend/ dir", () => {
    expect(build).toMatch(/--distpath[^\n]*backend/);
  });

  it("packages with DistributionTool when present, else a zip fallback", () => {
    expect(build).toContain("DistributionTool");
    expect(build).toMatch(/\bzip\b/);
  });
});

describe("packaging wiring", () => {
  it("exposes an npm package script that runs the build", () => {
    expect(pkg.scripts.package).toMatch(/build-plugin\.sh/);
  });

  it("gitignores the freeze output", () => {
    expect(sdIgnore).toMatch(/^backend\/$/m);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd streamdeck && npx vitest run tests/packaging.test.ts`
Expected: FAIL — `build-plugin.sh` missing, `pkg.scripts.package` undefined, `backend/` not ignored.

- [ ] **Step 3: Write minimal implementation**

```bash
# streamdeck/scripts/build-plugin.sh
#!/usr/bin/env bash
# Local, unsigned, arm64 build of the herdeck .streamDeckPlugin (frozen backend bundled).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # streamdeck/scripts
SD="$(cd "$HERE/.." && pwd)"                            # streamdeck
ROOT="$(cd "$SD/.." && pwd)"                            # repo root
PLUGIN="$SD/xyz.vaclavik.herdeck.sdPlugin"
ASSETS="$ROOT/src/herdeck/assets"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
OUT_NAME="xyz.vaclavik.herdeck.streamDeckPlugin"
OUT="$SD/$OUT_NAME"

echo "==> 1/4 pre-rasterize SVG -> PNG (build-time cairosvg)"
"$PY" -c "from herdeck.elgato.frozen import prerasterize_assets, BAKE_SIZE; \
print(prerasterize_assets('$ASSETS', '$ASSETS', BAKE_SIZE))"

echo "==> 2/4 freeze backend (PyInstaller onedir) into backend/"
"$PY" -m PyInstaller "$SD/herdeck-backend.spec" \
  --noconfirm \
  --distpath "$PLUGIN/backend" \
  --workpath "$ROOT/build/pyinstaller"

echo "==> 3/4 build TS shell -> bin/plugin.js"
( cd "$SD" && npm run build )

echo "==> 4/4 package .sdPlugin -> $OUT_NAME"
rm -f "$OUT"
if command -v DistributionTool >/dev/null 2>&1; then
  DistributionTool -b -i "$PLUGIN" -o "$SD"
else
  echo "    DistributionTool not found — using zip fallback (.streamDeckPlugin is a zip)"
  ( cd "$SD" && zip -r -X "$OUT_NAME" "$(basename "$PLUGIN")" >/dev/null )
fi
echo "Built: $OUT"
```

Make it executable: `chmod +x streamdeck/scripts/build-plugin.sh`.

Add the npm script (`streamdeck/package.json`):

```json
  "scripts": {
    "test": "vitest run",
    "build": "rollup -c",
    "package": "bash scripts/build-plugin.sh"
  },
```

Append `backend/` to `streamdeck/.gitignore`:

```
node_modules/
bin/
backend/
*.streamDeckPlugin
```

Append to the root `.gitignore` (build artifacts):

```
# Elgato packaging build artifacts
src/herdeck/assets/*.png
build/
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd streamdeck && npm test`
Expected: PASS (full TS suite, packaging gate green).

- [ ] **Step 5: Commit**

```bash
git add streamdeck/scripts/build-plugin.sh streamdeck/package.json streamdeck/.gitignore .gitignore streamdeck/tests/packaging.test.ts
git commit -m "feat: add local build-plugin pipeline and gitignore build artifacts"
```

---

## Task 8: Docs + full verification (incl. manual gate checklist)

**Files:**
- Modify: `README.md` (or `streamdeck/README.md` if one exists) — a "Packaging (local, unsigned)" section.
- Test: full suites + manual-gate checklist (no new automated test).

- [ ] **Step 1: Document the build + install flow**

Add a "Packaging the plugin (local, unsigned, arm64)" section covering:
- Prereqs: `.venv` with the `dev` + `elgato` extras (cairosvg + PyInstaller present); Node deps installed in `streamdeck/`.
- `cd streamdeck && npm run package` → produces `xyz.vaclavik.herdeck.streamDeckPlugin`.
- Install: double-click the artifact (or drag onto the Stream Deck app). It bundles the frozen backend, so no Python/herdeck install is needed.
- Note the discovery precedence (PI path → `HERDECK_BIN` → bundled → PATH) and that dev checkouts (no `backend/`) transparently fall through to a PATH `herdeck`.
- Note unsigned/ad-hoc: Gatekeeper may warn on machines other than the build host.

- [ ] **Step 2: Run the full Python suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — the prior 403 tests plus the new frozen/runtime/icons tests, all green.

- [ ] **Step 3: Run the full TS suite + type check**

Run: `cd streamdeck && npm test && npx tsc --noEmit`
Expected: PASS, tsc clean.

- [ ] **Step 4: Manual gate (local, on the dev Mac — not automated)**

Perform once and record the result in the commit body / report:
- `cd streamdeck && npm run package` completes and emits `xyz.vaclavik.herdeck.streamDeckPlugin`.
- Verify the freeze layout: `backend/herdeck-backend/herdeck-backend` exists and is executable.
- Smoke-run the frozen binary directly with the env contract and a throwaway socket, confirm it binds and serves without a Python on PATH (and without importing cairosvg). If PyInstaller reports a missing hidden import, add it to the `.spec` `hiddenimports` and re-freeze.
- Install the `.streamDeckPlugin` on a Mac with Stream Deck and confirm the plugin's tiles render (overlaps the existing on-hardware E2E follow-up).

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: document local packaging of the Elgato plugin"
```

---

## Self-Review (author's pass against the spec)

- **Spec part 1 (frozen icon rendering):** Tasks 1–3 (loader, baker, frozen session). cairosvg excluded in Task 6's `.spec`; offline fetch in Task 3. Font bundling (was Task 4) CUT — system fonts suffice on the target Mac. ✓
- **Spec part 2 (bundled discovery):** Task 5 — new precedence, existence check, `import.meta.url` resolution. ✓
- **Spec part 3 (build pipeline):** Tasks 6 (`.spec` + entry) + 7 (`build-plugin.sh`, npm script, gitignore). ✓
- **Testing section:** frozen-session/PNG-rasterizer/no-cairosvg (Tasks 1–3), TS precedence + path resolution (Task 5), build static gate (Tasks 6–7), manual install gate (Task 8). ✓
- **Risks:** font availability (resolved by CUT decision — system fonts), hidden imports (Task 6 `hiddenimports` + Task 8 manual verify; driver stack excluded), plugin-dir resolution from `import.meta.url` (Task 5), bundle size (accepted, slimmed by excluding the native driver stack). ✓
- **Type/name consistency:** `glyph_png_name`, `make_png_rasterizer`, `prerasterize_assets`, `baked_assets_dir`, `_frozen_session`, `_session_for_runtime`, `resolveHerdeckCommand`, `bundledBackendPath` — referenced consistently across tasks. ✓
- **Open redlines for the orchestrator:** the three flags at the top (onedir exe depth, cuttable font task, static-vs-real build test).
