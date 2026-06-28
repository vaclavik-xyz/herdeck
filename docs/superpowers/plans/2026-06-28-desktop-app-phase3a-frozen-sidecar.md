# Desktop app Phase 3a — frozen sidecar + installable macOS build — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `herdeck.app` runnable on an arm64 Mac with no Python/repo by bundling a PyInstaller-frozen `herdeck.deckapp` sidecar into the Tauri app and resolving it in production.

**Architecture:** Move the generic frozen-render helpers out of `elgato/` into a neutral `herdeck/frozen.py`; teach the deckapp's `_default_icons()` to use the Pillow-PNG rasterizer when frozen; add a PyInstaller onedir spec + build scripts that stage the bundle under `desktop/src-tauri/resources/herdeck-deckapp/`; teach the Rust shell to prefer that bundled binary over the dev `.venv`. A real headless freeze+smoke of the sidecar binary is the in-session gate; the GUI `.app` build is a manual user gate.

**Tech Stack:** Python (Pillow, PyInstaller 6 onedir), Rust (Tauri 2), bash build scripts.

## Global Constraints

Copied verbatim from `docs/superpowers/specs/2026-06-28-desktop-app-phase3a-frozen-sidecar-design.md`. Every task's requirements implicitly include these.

- **arm64-only, unsigned / ad-hoc.** No universal2, no signing/notarization (that is slice 3b).
- **onedir, not onefile** (no per-launch temp self-extraction).
- The frozen executable MUST end up at `<resource_dir>/herdeck-deckapp/herdeck-deckapp` — the staged path, the Tauri `bundle.resources` dest, and the Rust resolver path all agree on this.
- PyInstaller runs with `--distpath desktop/src-tauri/resources` (COLLECT adds the `herdeck-deckapp/` folder itself; do NOT pass `…/resources/herdeck-deckapp`).
- Frozen `_default_icons()` MUST pass BOTH `rasterize=make_png_rasterizer(baked_dir)` AND `assets_dir=baked_dir` (where `baked_dir = baked_assets_dir()`). The non-frozen path stays byte-for-byte behaviorally unchanged (cairosvg + default `_ASSETS_DIR`).
- `.spec` `excludes = ["cairosvg", "cffi", "cairocffi", "tkinter", "StreamDeck", "hid"]`. Never exclude `websockets`.
- Build env: `pip install -e '.[packaging]'`; this slice ADDS `tomli-w` to the `packaging` extra in `pyproject.toml`. Do NOT use the `deck` extra for the freeze.
- The discovery contract, token handling, and the existing `/state`·`/tile`·`/press`·`/config` proxy commands are unchanged. The access token stays Rust-side only.
- `bundle.targets` narrows to macOS `["app", "dmg"]`.

## Test runners (exact)

- Python: `.venv/bin/python -m pytest <path> -q`
- Ruff (BOTH dirs): `.venv/bin/ruff check src tests`
- Rust: `cd desktop/src-tauri && ~/.cargo/bin/cargo test`
- Frontend build (sanity, only if touched): `cd desktop && npm run build`
- Freeze gate (Task 4): `bash desktop/scripts/build-sidecar.sh` then `bash desktop/scripts/smoke-sidecar.sh`

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `src/herdeck/frozen.py` (create) | Neutral home for the generic frozen helpers | 1 |
| `src/herdeck/elgato/frozen.py` (modify) | Back-compat re-export shim → `herdeck.frozen` | 1 |
| `tests/test_frozen.py` (create) | Cover the neutral module surface | 1 |
| `src/herdeck/deckapp/server.py` (modify `_default_icons`) | Frozen-aware IconProvider wiring | 2 |
| `tests/test_deckapp_frozen_icons.py` (create) | Frozen vs non-frozen `_default_icons` | 2 |
| `desktop/scripts/deckapp-entry.py` (create) | PyInstaller entry → `herdeck.deckapp.__main__.main` | 3 |
| `desktop/herdeck-deckapp.spec` (create) | PyInstaller onedir spec for the sidecar | 3 |
| `desktop/scripts/build-sidecar.sh` (create) | pre-rasterize + freeze → staged onedir | 3 |
| `desktop/scripts/build-app.sh` (create) | build-sidecar.sh + `npm run tauri build` | 3 |
| `pyproject.toml` (modify) | Add `tomli-w` to `packaging` extra | 3 |
| `desktop/.gitignore` (modify) | Ignore the staged bundle | 3 |
| `tests/test_deckapp_entry.py` (create) | Entry emits a discovery line under dev python | 3 |
| `desktop/scripts/smoke-sidecar.sh` (create) | Headless smoke of a frozen binary | 4 |
| `desktop/src-tauri/src/sidecar.rs` (modify) | `resolve_frozen_sidecar` + `choose_spawn` (+ tests) | 5 |
| `desktop/src-tauri/src/lib.rs` (modify) | `resolve_plan(resource_dir)` + thread `resource_dir` | 6 |
| `desktop/src-tauri/tauri.conf.json` (modify) | `bundle.resources` + `bundle.targets` | 6 |

---

### Task 1: Move frozen helpers to a neutral `herdeck/frozen.py` + re-export shim

**Files:**
- Create: `src/herdeck/frozen.py`
- Modify: `src/herdeck/elgato/frozen.py` (replace body with a re-export shim)
- Test: `tests/test_frozen.py`

**Interfaces:**
- Produces (importable as `herdeck.frozen.*` AND, via the shim, `herdeck.elgato.frozen.*`):
  - `BAKE_SIZE: int`
  - `is_frozen() -> bool`
  - `baked_assets_dir() -> str`
  - `glyph_png_name(svg_text: str) -> str`
  - `make_png_rasterizer(baked_dir: str) -> Callable[[str, int], Image.Image]`
  - `prerasterize_assets(src_dir: str, out_dir: str, size: int = BAKE_SIZE) -> list[str]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_frozen.py`:

```python
import sys

from PIL import Image

from herdeck import frozen
from herdeck.icons import ICON_SIZE


def test_neutral_module_exposes_helpers():
    for name in (
        "BAKE_SIZE",
        "is_frozen",
        "baked_assets_dir",
        "glyph_png_name",
        "make_png_rasterizer",
        "prerasterize_assets",
    ):
        assert hasattr(frozen, name), name


def test_is_frozen_reflects_sys_frozen(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert frozen.is_frozen() is False
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert frozen.is_frozen() is True


def test_png_rasterizer_loads_prebaked_glyph(tmp_path):
    svg = "<svg>codex</svg>"
    Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (10, 20, 30, 255)).save(
        tmp_path / frozen.glyph_png_name(svg)
    )
    img = frozen.make_png_rasterizer(str(tmp_path))(svg, ICON_SIZE)
    assert img.mode == "RGBA" and img.size == (ICON_SIZE, ICON_SIZE)
    assert img.getpixel((0, 0)) == (10, 20, 30, 255)


def test_elgato_shim_reexports_same_objects():
    from herdeck.elgato import frozen as efrozen

    assert efrozen.is_frozen is frozen.is_frozen
    assert efrozen.make_png_rasterizer is frozen.make_png_rasterizer
    assert efrozen.BAKE_SIZE == frozen.BAKE_SIZE
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_frozen.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'herdeck.frozen'`.

- [ ] **Step 3: Create `src/herdeck/frozen.py`**

Move the current content of `src/herdeck/elgato/frozen.py` verbatim, changing only the relative import depth (`..icons` → `.icons`):

```python
from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Callable

from PIL import Image

from .icons import ICON_SIZE

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


def prerasterize_assets(src_dir: str, out_dir: str, size: int = BAKE_SIZE) -> list[str]:
    """Build-time: rasterize each ``*.svg`` in ``src_dir`` to a content-keyed PNG.

    Uses cairosvg (build-time only; excluded from the frozen bundle). Returns the
    baked PNG filenames (the bundle's glyph manifest). A PNG that already exists is
    left untouched — no re-encode, no mtime churn — so iterating only ``*.svg`` makes
    baking into the source assets dir (``out_dir == src_dir``) safe and idempotent.
    """
    import io

    import cairosvg  # build-time only — never bundled (see the .spec excludes)

    os.makedirs(out_dir, exist_ok=True)
    baked: list[str] = []
    for entry in sorted(os.listdir(src_dir)):
        if not entry.endswith(".svg"):
            continue
        with open(os.path.join(src_dir, entry), encoding="utf-8") as fh:
            svg = fh.read()
        name = glyph_png_name(svg)
        dst = os.path.join(out_dir, name)
        if not os.path.exists(dst):
            png = cairosvg.svg2png(bytestring=svg.encode(), output_width=size, output_height=size)
            Image.open(io.BytesIO(png)).convert("RGBA").save(dst)
        baked.append(name)
    return baked
```

- [ ] **Step 4: Replace `src/herdeck/elgato/frozen.py` with a re-export shim**

```python
"""Back-compat re-export shim.

The frozen-render helpers are generic (they depend only on ``herdeck.icons``),
so they now live in ``herdeck.frozen``. This module re-exports them unchanged so
``herdeck.elgato.runtime`` and any existing imports/tests keep working.
"""

from __future__ import annotations

from ..frozen import (
    BAKE_SIZE,
    baked_assets_dir,
    glyph_png_name,
    is_frozen,
    make_png_rasterizer,
    prerasterize_assets,
)

__all__ = [
    "BAKE_SIZE",
    "baked_assets_dir",
    "glyph_png_name",
    "is_frozen",
    "make_png_rasterizer",
    "prerasterize_assets",
]
```

- [ ] **Step 5: Run the new + existing frozen tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_frozen.py tests/test_elgato_frozen.py tests/test_elgato_runtime.py -q`
Expected: PASS (the existing `tests/test_elgato_frozen.py` exercises the shim; `tests/test_elgato_runtime.py` imports `from .frozen import ...` in `runtime.py`).

- [ ] **Step 6: Lint**

Run: `.venv/bin/ruff check src tests`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/herdeck/frozen.py src/herdeck/elgato/frozen.py tests/test_frozen.py
git commit -m "refactor: move frozen-render helpers to herdeck.frozen (elgato re-exports)"
```

---

### Task 2: Frozen-aware `_default_icons()` in the deckapp

**Files:**
- Modify: `src/herdeck/deckapp/server.py` (`_default_icons`, ~lines 413-427)
- Test: `tests/test_deckapp_frozen_icons.py`

**Interfaces:**
- Consumes: `herdeck.frozen.is_frozen`, `herdeck.frozen.baked_assets_dir`, `herdeck.frozen.make_png_rasterizer` (Task 1); `herdeck.icons.IconProvider(cache_dir, slug_map, fetch=..., rasterize=..., assets_dir=...)` and `herdeck.icons.DEFAULT_AGENT_SLUGS` (existing).
- Produces: `_default_icons()` returns an `IconProvider` whose `_assets_dir`/`_rasterize` are the frozen pair when `is_frozen()`, else the cairosvg defaults.

- [ ] **Step 1: Write the failing test**

Create `tests/test_deckapp_frozen_icons.py`:

```python
from PIL import Image

from herdeck import frozen
from herdeck.deckapp import server
from herdeck.icons import ICON_SIZE, _ASSETS_DIR


def test_default_icons_frozen_uses_baked_assets_and_png_rasterizer(tmp_path, monkeypatch):
    svg = "<svg>codex</svg>"
    Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (5, 6, 7, 255)).save(
        tmp_path / frozen.glyph_png_name(svg)
    )
    monkeypatch.setattr(frozen, "is_frozen", lambda: True)
    monkeypatch.setattr(frozen, "baked_assets_dir", lambda: str(tmp_path))

    icons = server._default_icons()

    assert icons._assets_dir == str(tmp_path)
    # The frozen rasterizer loads the pre-baked PNG (no cairosvg).
    img = icons._rasterize(svg, ICON_SIZE)
    assert img.size == (ICON_SIZE, ICON_SIZE)
    assert img.getpixel((0, 0)) == (5, 6, 7, 255)


def test_default_icons_non_frozen_keeps_cairosvg_defaults(monkeypatch):
    monkeypatch.setattr(frozen, "is_frozen", lambda: False)
    icons = server._default_icons()
    assert icons._assets_dir == _ASSETS_DIR  # default source-tree assets dir
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_deckapp_frozen_icons.py -q`
Expected: FAIL — `_default_icons()` ignores `is_frozen`, so `_assets_dir` equals `_ASSETS_DIR` in the frozen test.

- [ ] **Step 3: Implement the frozen branch**

Replace `_default_icons()` in `src/herdeck/deckapp/server.py` with:

```python
def _default_icons():
    """The shared IconProvider, configured for the mock: no network fetch, so the
    deck renders deterministically and offline (bundled SVG assets, else a letter
    glyph). Reuses herdeck.icons — no rendering logic is reimplemented here.

    When running frozen (PyInstaller bundle) there is no cairosvg, so glyphs are
    served from pre-baked PNGs: pass BOTH the PNG rasterizer and the bundled
    assets dir, matching the Elgato frozen session."""
    import os
    import tempfile

    from ..frozen import baked_assets_dir, is_frozen, make_png_rasterizer
    from ..icons import DEFAULT_AGENT_SLUGS, IconProvider

    cache = os.path.join(tempfile.gettempdir(), "herdeck-deckapp-icons")
    if is_frozen():
        baked = baked_assets_dir()
        return IconProvider(
            cache_dir=cache,
            slug_map=DEFAULT_AGENT_SLUGS,
            fetch=lambda slug: None,  # offline-first when frozen
            rasterize=make_png_rasterizer(baked),
            assets_dir=baked,
        )
    return IconProvider(
        cache_dir=cache,
        slug_map=DEFAULT_AGENT_SLUGS,
        fetch=lambda slug: None,  # mock stays offline + deterministic
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_deckapp_frozen_icons.py -q`
Expected: PASS.

- [ ] **Step 5: Run the deckapp suite to confirm no regression**

Run: `.venv/bin/python -m pytest tests/test_deckapp.py -q && .venv/bin/ruff check src tests`
Expected: PASS, no lint errors.

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/deckapp/server.py tests/test_deckapp_frozen_icons.py
git commit -m "feat: deckapp _default_icons uses Pillow-PNG rasterizer when frozen"
```

---

### Task 3: Packaging scaffolding (entry, spec, build scripts, deps, gitignore)

**Files:**
- Create: `desktop/scripts/deckapp-entry.py`
- Create: `desktop/herdeck-deckapp.spec`
- Create: `desktop/scripts/build-sidecar.sh`
- Create: `desktop/scripts/build-app.sh`
- Modify: `pyproject.toml` (add `tomli-w` to the `packaging` extra)
- Modify: `desktop/.gitignore` (ignore the staged bundle)
- Test: `tests/test_deckapp_entry.py`

**Interfaces:**
- Consumes: `herdeck.deckapp.__main__.main` (existing), `herdeck.frozen.prerasterize_assets` (Task 1).
- Produces: the build script writes the frozen onedir to `desktop/src-tauri/resources/herdeck-deckapp/herdeck-deckapp` (consumed by Tasks 4 + 5/6).

- [ ] **Step 1: Write the failing test**

Create `tests/test_deckapp_entry.py`:

```python
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENTRY = REPO / "desktop" / "scripts" / "deckapp-entry.py"


def test_entry_emits_a_discovery_line():
    assert ENTRY.exists(), "deckapp entry script missing"
    env = {**os.environ, "PYTHONPATH": str(REPO / "src"), "HERDECK_MOCK": "1"}
    proc = subprocess.Popen(
        [sys.executable, str(ENTRY)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    try:
        line = proc.stdout.readline()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    d = json.loads(line)
    assert set(d) >= {"url", "host", "port", "token", "source"}
    assert d["source"] == "mock"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_deckapp_entry.py -q`
Expected: FAIL — entry script missing.

- [ ] **Step 3: Create the entry script**

Create `desktop/scripts/deckapp-entry.py`:

```python
"""PyInstaller entry for the frozen herdeck.deckapp sidecar.

PyInstaller analyses a real script file (not a ``-m`` target), so this thin
wrapper just delegates to the existing module main, which prints the discovery
JSON line and serves the loopback HTTP API.
"""

import sys

from herdeck.deckapp.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the entry test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_deckapp_entry.py -q`
Expected: PASS (runs under the dev interpreter; proves the entry independent of PyInstaller).

- [ ] **Step 5: Create the PyInstaller spec**

Create `desktop/herdeck-deckapp.spec`:

```python
# PyInstaller spec — arm64 onedir frozen herdeck.deckapp sidecar for the desktop app.
# Build via desktop/scripts/build-sidecar.sh, e.g.:
#   pyinstaller desktop/herdeck-deckapp.spec --noconfirm \
#     --distpath desktop/src-tauri/resources --workpath build/pyinstaller-deckapp
# COLLECT(name="herdeck-deckapp") itself creates the herdeck-deckapp/ folder UNDER
# --distpath, so the exe lands at <distpath>/herdeck-deckapp/herdeck-deckapp.
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # repo root (SPECPATH = desktop/)

a = Analysis(
    [os.path.join(SPECPATH, "scripts", "deckapp-entry.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=[],
    # Bundle the assets dir (SVG glyphs + the pre-baked PNGs the baker writes into
    # it) as herdeck_assets. baked_assets_dir() resolves to it via sys._MEIPASS.
    datas=[(os.path.join(ROOT, "src", "herdeck", "assets"), "herdeck_assets")],
    # deckapp graph: the source/live/mock paths + the WS bridge client. websockets
    # is a CORE dep (connector imports it at module top). tomli_w is imported at the
    # top of deckapp.config_service; listed as a safety net against the lazy path.
    hiddenimports=[
        "herdeck.deckapp.server",
        "herdeck.deckapp.live",
        "herdeck.deckapp.mock",
        "herdeck.deckapp.source",
        "herdeck.deckapp.watcher",
        "herdeck.deckapp.config_service",
        "websockets",
        "tomli_w",
    ],
    # cairosvg (+ native cffi/cairocffi) is build-time only — the frozen deckapp
    # uses the Pillow PNG rasterizer. Drop the HID driver stack (StreamDeck + hid):
    # the deckapp reaches herdr only through the bridge WS, never USB. NEVER drop
    # websockets. Add to excludes ONLY graph-unreachable native deps.
    excludes=["cairosvg", "cffi", "cairocffi", "tkinter", "StreamDeck", "hid"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="herdeck-deckapp",
    console=True,
    target_arch="arm64",
)
coll = COLLECT(exe, a.binaries, a.datas, name="herdeck-deckapp")
```

- [ ] **Step 6: Create the freeze build script**

Create `desktop/scripts/build-sidecar.sh`:

```bash
#!/usr/bin/env bash
# Freeze the herdeck.deckapp sidecar into an arm64 onedir bundle for the Tauri app.
# Prereq: a Python env with the `packaging` extra:  pip install -e '.[packaging]'
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # desktop/scripts
DESKTOP="$(dirname "$HERE")"                            # desktop
ROOT="$(dirname "$DESKTOP")"                            # repo root
PY="${HERDECK_PY:-$ROOT/.venv/bin/python}"

DIST="$DESKTOP/src-tauri/resources"
WORK="$ROOT/build/pyinstaller-deckapp"
ASSETS="$ROOT/src/herdeck/assets"

echo "==> 1/3 pre-rasterize SVG glyphs -> PNG (into the bundled assets dir)"
"$PY" -c "from herdeck.frozen import prerasterize_assets; print(prerasterize_assets('$ASSETS', '$ASSETS'))"

echo "==> 2/3 freeze deckapp sidecar (PyInstaller onedir)"
"$PY" -m PyInstaller "$DESKTOP/herdeck-deckapp.spec" --noconfirm \
  --distpath "$DIST" --workpath "$WORK"

echo "==> 3/3 verify staged artifact"
BIN="$DIST/herdeck-deckapp/herdeck-deckapp"
test -x "$BIN" || { echo "FAIL: $BIN missing or not executable"; exit 1; }
# PyInstaller 6 onedir places datas under _internal/.
test -d "$DIST/herdeck-deckapp/_internal/herdeck_assets" \
  || test -d "$DIST/herdeck-deckapp/herdeck_assets" \
  || { echo "FAIL: bundled herdeck_assets missing"; exit 1; }
echo "OK: $BIN"
```

- [ ] **Step 7: Create the full-app build script**

Create `desktop/scripts/build-app.sh`:

```bash
#!/usr/bin/env bash
# Build the full herdeck.app: freeze the sidecar, then run the Tauri GUI build.
# This needs a desktop session + the GUI toolchain (it does NOT run headless).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # desktop/scripts
DESKTOP="$(dirname "$HERE")"                            # desktop

bash "$HERE/build-sidecar.sh"

echo "==> tauri build (.app + .dmg)"
cd "$DESKTOP"
npm run tauri build
```

- [ ] **Step 8: Make the scripts executable + syntax-check them**

```bash
chmod +x desktop/scripts/build-sidecar.sh desktop/scripts/build-app.sh
bash -n desktop/scripts/build-sidecar.sh && bash -n desktop/scripts/build-app.sh
```

Expected: no output (syntax OK).

- [ ] **Step 9: Add `tomli-w` to the `packaging` extra**

In `pyproject.toml`, change the `packaging` extra line from:

```toml
packaging = ["pyinstaller>=6", "cairosvg", "pillow>=10", "websockets>=14", "keyring"]
```

to (adds `tomli-w`, the only deckapp-frozen dep `packaging` lacked):

```toml
packaging = ["pyinstaller>=6", "cairosvg", "pillow>=10", "websockets>=14", "keyring", "tomli-w"]
```

- [ ] **Step 10: Ignore the staged bundle**

Append to `desktop/.gitignore` under the `# rust / tauri` section:

```
src-tauri/resources/herdeck-deckapp/
```

(The baked PNGs `src/herdeck/assets/*.png` and `build/` are already ignored by the root `.gitignore`.)

- [ ] **Step 11: Verify pyproject parses + entry test still green**

Run: `.venv/bin/python -c "import tomllib,pathlib; tomllib.loads(pathlib.Path('pyproject.toml').read_text())" && .venv/bin/python -m pytest tests/test_deckapp_entry.py -q && .venv/bin/ruff check src tests`
Expected: no error, test PASS, no lint errors.

- [ ] **Step 12: Commit**

```bash
git add desktop/scripts/deckapp-entry.py desktop/herdeck-deckapp.spec \
  desktop/scripts/build-sidecar.sh desktop/scripts/build-app.sh \
  pyproject.toml desktop/.gitignore tests/test_deckapp_entry.py
git commit -m "build: PyInstaller spec + scripts to freeze the deckapp sidecar"
```

---

### Task 4: Real freeze + headless smoke gate

**Files:**
- Create: `desktop/scripts/smoke-sidecar.sh`
- May modify (only if the smoke reveals a gap): `desktop/herdeck-deckapp.spec` `hiddenimports`/`excludes`

**Interfaces:**
- Consumes: the staged binary from Task 3 (`desktop/src-tauri/resources/herdeck-deckapp/herdeck-deckapp`).
- Produces: a committed, reusable smoke script. The in-session run of build + smoke is the slice's verification gate.

**Note for the implementer:** This task ACTUALLY runs PyInstaller (minutes) and spawns the frozen binary. First ensure the build env: `.venv/bin/pip install -e '.[packaging]'` (pulls `pyinstaller`, `cairosvg`, `pillow`, `websockets`, `keyring`, and now `tomli-w`). If the smoke fails on a missing import, add the named module to the `.spec` `hiddenimports` and re-freeze — this is the expected freeze-debug loop, not scope creep (the spec mandates "add more only if the real smoke run shows PyInstaller missed a reachable import"). Record any addition in the report.

- [ ] **Step 1: Write the smoke script**

Create `desktop/scripts/smoke-sidecar.sh`:

```bash
#!/usr/bin/env bash
# Headless smoke of the FROZEN deckapp sidecar: spawn it, read its discovery line,
# and assert it serves the token-authed loopback API (mock source — no config).
# Usage: bash desktop/scripts/smoke-sidecar.sh [path/to/herdeck-deckapp]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP="$(dirname "$HERE")"
BIN="${1:-$DESKTOP/src-tauri/resources/herdeck-deckapp/herdeck-deckapp}"
test -x "$BIN" || { echo "FAIL: frozen binary not found at $BIN"; exit 1; }

# Force the deterministic mock source (no on-disk config / keychain needed).
export HERDECK_MOCK=1

LINE_FILE="$(mktemp)"
"$BIN" >"$LINE_FILE" 2>/dev/null &
PID=$!
trap 'kill "$PID" 2>/dev/null || true' EXIT

# Wait up to ~10s for the discovery line.
for _ in $(seq 1 100); do
  [ -s "$LINE_FILE" ] && break
  sleep 0.1
done
DISCOVERY="$(head -n1 "$LINE_FILE")"
[ -n "$DISCOVERY" ] || { echo "FAIL: no discovery line"; exit 1; }
echo "discovery: $DISCOVERY"

# Parse host/port/token without jq (python3 is always present on macOS).
read -r HOST PORT TOKEN <<EOF
$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d['host'], d['port'], d['token'])" "$DISCOVERY")
EOF

check() {  # check <name> <expected-status> <path-with-token>
  local name="$1" want="$2" path="$3"
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://$HOST:$PORT$path")"
  if [ "$code" != "$want" ]; then echo "FAIL: $name -> HTTP $code (want $want)"; exit 1; fi
  echo "OK: $name -> $code"
}

check health 200 "/health?token=$TOKEN"
check tile   200 "/tile/0?token=$TOKEN"
check config 200 "/config?token=$TOKEN"
echo "SMOKE PASS"
```

- [ ] **Step 2: Make it executable + syntax-check**

```bash
chmod +x desktop/scripts/smoke-sidecar.sh
bash -n desktop/scripts/smoke-sidecar.sh
```

Expected: no output.

- [ ] **Step 3: Ensure the build env, then run the REAL freeze**

```bash
.venv/bin/pip install -e '.[packaging]'
bash desktop/scripts/build-sidecar.sh
```

Expected: ends with `OK: …/resources/herdeck-deckapp/herdeck-deckapp`. If PyInstaller errors on a missing module, add it to the `.spec` `hiddenimports` and re-run.

- [ ] **Step 4: Run the headless smoke against the frozen binary**

Run: `bash desktop/scripts/smoke-sidecar.sh`
Expected: prints the discovery line, `OK: health -> 200`, `OK: tile -> 200`, `OK: config -> 200`, `SMOKE PASS`.

If `/tile` is non-200 or the process dies, the frozen render path is broken (missing assets/rasterizer) — fix the `.spec` datas / the Task 2 wiring before continuing. If `/config` is non-200, `tomli_w` is likely unbundled — confirm it is in `hiddenimports` and installed.

- [ ] **Step 5: Confirm the staged bundle stays gitignored**

Run: `git status --porcelain desktop/src-tauri/resources`
Expected: empty (the staged onedir is ignored by Task 3's `.gitignore` line).

- [ ] **Step 6: Commit the smoke script (+ any spec hiddenimport fix)**

```bash
git add desktop/scripts/smoke-sidecar.sh desktop/herdeck-deckapp.spec
git commit -m "test: headless smoke gate for the frozen deckapp sidecar"
```

(If the `.spec` was unchanged, omit it from the `git add`.)

---

### Task 5: Rust frozen-sidecar resolution (`sidecar.rs`)

**Files:**
- Modify: `desktop/src-tauri/src/sidecar.rs` (add two pub fns + a `#[cfg(test)]` block)

**Interfaces:**
- Consumes: `CommandSpec` (existing struct), `resolve_dev_sidecar(repo_root: &Path) -> CommandSpec` (existing).
- Produces:
  - `resolve_frozen_sidecar(resource_dir: &Path) -> Option<CommandSpec>` — `Some` iff `<resource_dir>/herdeck-deckapp/herdeck-deckapp` is a file.
  - `choose_spawn(resource_dir: Option<&Path>, repo_root: &Path) -> CommandSpec` — frozen if present, else dev `.venv`.

- [ ] **Step 1: Write the failing tests**

Add to the `#[cfg(test)] mod tests` block in `desktop/src-tauri/src/sidecar.rs`:

```rust
    fn scratch(name: &str) -> PathBuf {
        // Dependency-free temp dir keyed by the (unique) test name.
        let p = std::env::temp_dir().join(format!("herdeck-3a-{name}"));
        let _ = std::fs::remove_dir_all(&p);
        std::fs::create_dir_all(&p).unwrap();
        p
    }

    fn stage_frozen(resource_dir: &Path) -> PathBuf {
        let dir = resource_dir.join("herdeck-deckapp");
        std::fs::create_dir_all(&dir).unwrap();
        let bin = dir.join("herdeck-deckapp");
        std::fs::write(&bin, b"#!/bin/sh\n").unwrap();
        bin
    }

    #[test]
    fn resolve_frozen_sidecar_some_when_binary_exists() {
        let res = scratch("frozen-exists");
        let bin = stage_frozen(&res);
        let spec = resolve_frozen_sidecar(&res).expect("should resolve");
        assert_eq!(spec.program, bin.to_string_lossy());
        assert!(spec.args.is_empty());
        assert!(spec.cwd.is_none());
        assert!(spec.envs.is_empty());
    }

    #[test]
    fn resolve_frozen_sidecar_none_when_missing() {
        let res = scratch("frozen-missing");
        assert!(resolve_frozen_sidecar(&res).is_none());
    }

    #[test]
    fn choose_spawn_prefers_frozen_then_falls_back_to_dev_venv() {
        let res = scratch("choose-frozen");
        stage_frozen(&res);
        let frozen = choose_spawn(Some(&res), Path::new("/repo"));
        assert!(frozen.program.ends_with("/herdeck-deckapp/herdeck-deckapp"));

        // Empty resource dir -> no bundle -> dev venv.
        let empty = scratch("choose-empty");
        let dev = choose_spawn(Some(&empty), Path::new("/repo"));
        assert!(dev.program.ends_with("/.venv/bin/python"));

        // No resource dir at all -> dev venv.
        let none = choose_spawn(None, Path::new("/repo"));
        assert!(none.program.ends_with("/.venv/bin/python"));
    }
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd desktop/src-tauri && ~/.cargo/bin/cargo test resolve_frozen_sidecar`
Expected: FAIL — `cannot find function resolve_frozen_sidecar`.

- [ ] **Step 3: Implement the two functions**

Add to `desktop/src-tauri/src/sidecar.rs` (after `resolve_dev_sidecar`):

```rust
/// Resolve the frozen/bundled sidecar command from the Tauri resource dir. The
/// PyInstaller onedir bundle lands at `<resource_dir>/herdeck-deckapp/` with the
/// executable inside it. Returns `Some` only when that binary actually exists, so
/// a dev build (no staged bundle) cleanly falls through to the `.venv`.
pub fn resolve_frozen_sidecar(resource_dir: &Path) -> Option<CommandSpec> {
    let bin = resource_dir.join("herdeck-deckapp").join("herdeck-deckapp");
    if bin.is_file() {
        Some(CommandSpec {
            program: bin.to_string_lossy().into_owned(),
            args: vec![],
            cwd: None,
            envs: vec![],
        })
    } else {
        None
    }
}

/// Pick the sidecar to spawn: the bundled frozen binary when present (production),
/// otherwise the dev `.venv` interpreter. `resource_dir` is `None` when Tauri
/// could not resolve one (then we always use the dev path).
pub fn choose_spawn(resource_dir: Option<&Path>, repo_root: &Path) -> CommandSpec {
    if let Some(dir) = resource_dir {
        if let Some(spec) = resolve_frozen_sidecar(dir) {
            return spec;
        }
    }
    resolve_dev_sidecar(repo_root)
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd desktop/src-tauri && ~/.cargo/bin/cargo test`
Expected: PASS — the new tests plus the existing `sidecar`/`spawn`/`http` tests.

- [ ] **Step 5: Commit**

```bash
git add desktop/src-tauri/src/sidecar.rs
git commit -m "feat: resolve_frozen_sidecar + choose_spawn (prefer bundled, fall back to .venv)"
```

---

### Task 6: Wire production resolution + bundle config

**Files:**
- Modify: `desktop/src-tauri/src/lib.rs` (`resolve_plan`, `start_sidecar`)
- Modify: `desktop/src-tauri/tauri.conf.json` (`bundle.resources`, `bundle.targets`)

**Interfaces:**
- Consumes: `sidecar::choose_spawn(resource_dir: Option<&Path>, repo_root: &Path)` (Task 5).
- Produces: the running app spawns the bundled sidecar in production, the dev `.venv` otherwise. (No new public surface; this is the keystone integration.)

- [ ] **Step 1: Thread `resource_dir` through `resolve_plan`**

In `desktop/src-tauri/src/lib.rs`, change `resolve_plan` to take the resource dir and use `choose_spawn` for the spawn branch. Replace the current signature/spawn tail:

```rust
fn resolve_plan() -> SidecarPlan {
```
…
```rust
    SidecarPlan::Spawn(resolve_dev_sidecar(&repo_root_from_manifest()))
}
```

with:

```rust
fn resolve_plan(resource_dir: Option<PathBuf>) -> SidecarPlan {
```
…
```rust
    SidecarPlan::Spawn(sidecar::choose_spawn(
        resource_dir.as_deref(),
        &repo_root_from_manifest(),
    ))
}
```

Also update the import at the top of `lib.rs` — it currently brings in `resolve_dev_sidecar`; add `choose_spawn` (and drop `resolve_dev_sidecar` from the `use` if it becomes unused):

```rust
use sidecar::{choose_spawn, supervise, CommandSpec, Discovery, SupervisorConfig};
```

(Keep `CommandSpec`, `Discovery`, `SupervisorConfig`, `supervise` — still used. If the compiler warns `resolve_dev_sidecar` is unused, remove it from the `use`; `choose_spawn` calls it internally in `sidecar.rs`.)

- [ ] **Step 2: Pass the resource dir from `start_sidecar`**

In `start_sidecar` (which already has `app: &tauri::App`), compute the resource dir and pass it. Change the `match resolve_plan() {` line to:

```rust
    let resource_dir = app.path().resource_dir().ok();
    match resolve_plan(resource_dir) {
```

`app.path()` is available via the already-imported `tauri::Manager` trait; `resource_dir()` returns `tauri::Result<PathBuf>`, so `.ok()` yields `Option<PathBuf>`.

- [ ] **Step 3: Build to verify it compiles + tests pass**

Run: `cd desktop && npm run build && cd src-tauri && ~/.cargo/bin/cargo test`
Expected: frontend build OK (unchanged), `cargo` compiles, all tests pass. Fix any unused-import warning per Step 1.

- [ ] **Step 4: Add `bundle.resources` + narrow `bundle.targets`**

In `desktop/src-tauri/tauri.conf.json`, replace the `bundle` block:

```json
  "bundle": {
    "active": true,
    "targets": "all",
    "icon": [
      "icons/32x32.png",
      "icons/128x128.png",
      "icons/128x128@2x.png",
      "icons/icon.png"
    ]
  }
```

with:

```json
  "bundle": {
    "active": true,
    "targets": ["app", "dmg"],
    "resources": {
      "resources/herdeck-deckapp": "herdeck-deckapp"
    },
    "icon": [
      "icons/32x32.png",
      "icons/128x128.png",
      "icons/128x128@2x.png",
      "icons/icon.png"
    ]
  }
```

The map form stages `src-tauri/resources/herdeck-deckapp/` into the bundle's resource dir as `herdeck-deckapp/`, so the executable resolves at `<resource_dir>/herdeck-deckapp/herdeck-deckapp` — exactly what `resolve_frozen_sidecar` expects.

- [ ] **Step 5: Validate the config JSON + re-run the build**

Run: `.venv/bin/python -c "import json,pathlib; json.loads(pathlib.Path('desktop/src-tauri/tauri.conf.json').read_text())" && cd desktop && npm run build && cd src-tauri && ~/.cargo/bin/cargo build`
Expected: JSON valid, frontend + cargo build OK. (A full `tauri build` is the manual user gate — not run here.)

- [ ] **Step 6: Commit**

```bash
git add desktop/src-tauri/src/lib.rs desktop/src-tauri/tauri.conf.json
git commit -m "feat: spawn bundled sidecar in production + bundle it as a Tauri resource"
```

---

## Self-Review

**1. Spec coverage**
- Frozen-safe rendering for the deckapp (spec §1) → Tasks 1 + 2.
- Frozen sidecar bundle / PyInstaller spec (spec §2) → Task 3 + Task 4 (real freeze).
- Bundling + production resolution in Rust/Tauri (spec §3) → Tasks 5 + 6.
- Build pipeline + build env + gitignore (spec §4) → Task 3 (scaffold) + Task 4 (real run).
- Testing — Python TDD, Rust TDD, build-artifact check, real freeze+smoke gate, manual `.app` gate (spec "Testing") → Tasks 1/2 (Py), 5 (Rust), 3 step 8 + 4 step 1/3 (artifact + smoke), Task 4 (gate); manual `.app` gate documented in spec out-of-scope (user runs `build-app.sh`).
- Risks (font, hidden imports, resource_dir dev-vs-bundle, keychain) → handled: font/hidden-imports surfaced by the Task 4 smoke; resource_dir fall-through covered by Task 5 `choose_spawn` tests; keychain stays out of automated scope (smoke is mock-only).

**2. Placeholder scan** — no TBD/TODO; every code step shows full code; commands have expected output.

**3. Type/name consistency**
- `resolve_frozen_sidecar(&Path) -> Option<CommandSpec>` and `choose_spawn(Option<&Path>, &Path) -> CommandSpec` defined in Task 5, consumed identically in Task 6.
- `baked_assets_dir`/`make_png_rasterizer`/`is_frozen`/`prerasterize_assets` defined in Task 1, used with the same signatures in Tasks 2 + 3.
- Staged path `desktop/src-tauri/resources/herdeck-deckapp/herdeck-deckapp` is identical across the `.spec`/`--distpath` (Task 3), the smoke default (Task 4), the resolver (Task 5), and the Tauri `resources` dest (Task 6).
- `_default_icons()` frozen branch passes both `rasterize=` and `assets_dir=` per the Global Constraints.
