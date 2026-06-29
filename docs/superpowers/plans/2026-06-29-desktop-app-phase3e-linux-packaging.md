# Phase 3e: Linux packaging — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce installable Linux x86_64 artifacts (AppImage + .deb + .rpm) of the herdeck desktop app — a Linux-frozen sidecar bundled by Tauri — built and verified in GitHub Actions CI, without regressing the macOS build.

**Architecture:** Three independent, narrowly-scoped changes. (1) Make the PyInstaller spec arch-agnostic so it freezes for the host arch on both macOS-arm64 and Linux-x86_64. (2) Make the Tauri bundle config cross-platform (`targets: "all"` + Linux desktop metadata). (3) Add a new `release.yml` CI workflow that, on a Linux runner, freezes the sidecar and bundles AppImage/deb/rpm — the only place the Linux build can actually be produced and verified.

**Tech Stack:** PyInstaller 6 (onedir freeze), Tauri 2 (Rust bundler → appimage/deb/rpm), GitHub Actions (`ubuntu-24.04` runner), bash build scripts.

## Global Constraints

Every task implicitly includes these (copied verbatim from the spec):

- **Komunikace** lidská česky, **kód a commit messages anglicky**; conventional commits; **žádné `Co-Authored-By`**; po commitu zkontrolovat `roborev show <sha>`.
- **Push/PR/merge jen s explicitním souhlasem uživatele.**
- **macOS nesmí regresovat** — po změně PyInstaller specu MUSÍ na dev Macu projít `build-sidecar.sh` + `smoke-sidecar.sh` (host=arm64 freeze beze změny chování).
- **Cílová arch Linuxu = x86_64**; runner `ubuntu-24.04`. arm64 explicitně mimo.
- **Linux targets:** AppImage **+** .deb **+** .rpm (všechny tři).
- **CI trigger:** `workflow_dispatch` **+** `push: tags: ['v*']` (NE každý push).
- **Testy:** Python `.venv/bin/python -m pytest`, lint `.venv/bin/ruff check src tests` (OBĚ složky); Rust `cd desktop/src-tauri && cargo test`; freeze gate `bash desktop/scripts/build-sidecar.sh` + `smoke-sidecar.sh`.

## Testing note (read before reviewing any task)

This is a **build-system + CI slice**. The spec states explicitly: *"Žádné nové
Python/TS/Rust unit testy se nečekají — logika beze změny."* There is no new
runtime logic to unit-test — the changes are a PyInstaller config value, a JSON
bundle config, build-script comments, and a CI workflow YAML. The **gate for
each task is a real verification command** (freeze+smoke, JSON parse, YAML
parse), not a `pytest`/`cargo test` unit test. A reviewer should NOT treat the
absence of a new unit test as a defect for these tasks; they should verify the
task's stated verification command was run and passed. The ultimate Linux proof
is the CI job (Task 3), run post-merge via `gh workflow run release.yml`.

The existing suites (`pytest`, `cargo test`, `npm test`) must remain green —
these changes touch none of their code, so they serve as regression guards.

---

### Task 1: Cross-platform freeze (drop hard-coded arch + neutralize build-script wording)

**Files:**
- Modify: `desktop/herdeck-deckapp.spec` (header comment line 1; EXE `target_arch`)
- Modify: `desktop/scripts/build-sidecar.sh` (header comment line 2)
- Modify: `desktop/scripts/build-app.sh` (header comment + echo)

**Interfaces:**
- Consumes: nothing (first task).
- Produces: a PyInstaller spec that freezes for the **host arch** (arm64 on the
  dev Mac, x86_64 on the Linux CI runner). The frozen onedir layout
  (`herdeck-deckapp/herdeck-deckapp` + `_internal/herdeck_assets`) is unchanged —
  later tasks and the existing Rust `resolve_frozen_sidecar` rely on it as-is.

**Why no unit test:** dropping a hard-coded build target is a config value, not
logic. The verification is the real freeze+smoke gate on this Mac, which proves
the host-arch (arm64) freeze still works — i.e. macOS does not regress. The
Linux x86_64 freeze is verified in Task 3 (CI).

- [ ] **Step 1: Establish the baseline — current freeze+smoke is green**

Run:
```bash
bash desktop/scripts/build-sidecar.sh && bash desktop/scripts/smoke-sidecar.sh
```
Expected: ends with `OK: ...` lines and `SMOKE PASS`. (This is the pre-change
baseline; if it is already red, STOP and report — the environment is broken, not
your change.)

- [ ] **Step 2: Drop the hard-coded `target_arch` in the PyInstaller spec**

In `desktop/herdeck-deckapp.spec`, the `EXE(...)` call currently ends:

```python
    exclude_binaries=True,
    name="herdeck-deckapp",
    console=True,
    target_arch="arm64",
)
```

Replace the `target_arch="arm64",` line with a comment (no value → host arch):

```python
    exclude_binaries=True,
    name="herdeck-deckapp",
    console=True,
    # No target_arch -> PyInstaller freezes for the HOST arch: arm64 on the dev
    # Mac, x86_64 on the Linux CI runner. One spec serves both OSes (3e).
)
```

- [ ] **Step 3: Neutralize the arch wording in the spec header comment**

In `desktop/herdeck-deckapp.spec`, line 1 currently reads:

```python
# PyInstaller spec — arm64 onedir frozen herdeck.deckapp sidecar for the desktop app.
```

Change `arm64` → `host-arch`:

```python
# PyInstaller spec — host-arch onedir frozen herdeck.deckapp sidecar for the desktop app.
```

- [ ] **Step 4: Neutralize the arch wording in `build-sidecar.sh`**

In `desktop/scripts/build-sidecar.sh`, line 2 currently reads:

```bash
# Freeze the herdeck.deckapp sidecar into an arm64 onedir bundle for the Tauri app.
```

Change to:

```bash
# Freeze the herdeck.deckapp sidecar into a host-arch onedir bundle for the Tauri app.
```

- [ ] **Step 5: Neutralize the macOS-only wording in `build-app.sh`**

In `desktop/scripts/build-app.sh`, change the header comment and the echo so the
script reads as OS-neutral (it already just calls `build-sidecar.sh` +
`npm run tauri build`, which pick the right targets per OS via Task 2's config):

Header comment line 2, currently:
```bash
# Build the full herdeck.app: freeze the sidecar, then run the Tauri GUI build.
```
→
```bash
# Build the full herdeck desktop app: freeze the sidecar, then run the Tauri GUI build.
```

Echo line, currently:
```bash
echo "==> tauri build (.app + .dmg)"
```
→
```bash
echo "==> tauri build (native bundles for the host OS)"
```

- [ ] **Step 6: Verify the freeze+smoke is STILL green after the change (macOS no-regression gate)**

Run:
```bash
bash desktop/scripts/build-sidecar.sh && bash desktop/scripts/smoke-sidecar.sh
```
Expected: `SMOKE PASS` again. This proves dropping the explicit arch did not
change the host (arm64) freeze — the macOS build path is intact.

- [ ] **Step 7: Confirm the frozen binary is the host arch (sanity)**

Run:
```bash
file desktop/src-tauri/resources/herdeck-deckapp/herdeck-deckapp
```
Expected on this Mac: output contains `arm64` (Mach-O arm64). On the Linux runner
the same command would show `x86-64` — that is Task 3's job, not verifiable here.

- [ ] **Step 8: Commit**

```bash
git add desktop/herdeck-deckapp.spec desktop/scripts/build-sidecar.sh desktop/scripts/build-app.sh
git commit -m "build(desktop): freeze sidecar for host arch (drop hard-coded arm64)"
```

---

### Task 2: Cross-platform Tauri bundle config (`targets: all` + Linux desktop metadata)

**Files:**
- Modify: `desktop/src-tauri/tauri.conf.json` (`bundle` block)

**Interfaces:**
- Consumes: the frozen onedir bundle staged by Task 1 (referenced unchanged via
  `bundle.resources`).
- Produces: a bundle config that yields `app`+`dmg` on macOS (identical to today)
  and `appimage`+`deb`+`rpm` on Linux. Task 3's CI job relies on `targets: "all"`
  producing the three Linux bundles.

**Why no unit test:** this is a static JSON config with no logic. The local
verification is: the JSON parses, and the new keys are present and correct. The
behavioral proof (3 Linux bundles produced; macOS app+dmg unchanged) lives in
Task 3's CI run and the optional macOS `tauri build` gate respectively.

- [ ] **Step 1: Verify the config currently parses (baseline)**

Run:
```bash
python3 -c "import json; json.load(open('desktop/src-tauri/tauri.conf.json')); print('JSON OK')"
```
Expected: `JSON OK`.

- [ ] **Step 2: Rewrite the `bundle` block to be cross-platform**

In `desktop/src-tauri/tauri.conf.json`, the `bundle` block currently is:

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
      "icons/icon.png",
      "icons/icon.icns"
    ]
  }
```

Replace it with (changes: `targets` → `"all"`; add `category`; add `linux.deb.depends`):

```json
  "bundle": {
    "active": true,
    "targets": "all",
    "category": "Utility",
    "resources": {
      "resources/herdeck-deckapp": "herdeck-deckapp"
    },
    "icon": [
      "icons/32x32.png",
      "icons/128x128.png",
      "icons/128x128@2x.png",
      "icons/icon.png",
      "icons/icon.icns"
    ],
    "linux": {
      "deb": {
        "depends": ["libayatana-appindicator3-1"]
      }
    }
  }
```

Rationale (do not add to the file — context for the implementer/reviewer):
- `"targets": "all"` lets Tauri pick the targets valid for the host OS: macOS →
  `app`+`dmg` (identical to the old explicit list), Linux → `appimage`+`deb`+`rpm`.
- `"category": "Utility"` populates the generated `.desktop` `Categories` (Linux)
  and `LSApplicationCategoryType` (macOS); harmless on macOS.
- `linux.deb.depends` adds the tray's runtime lib to the `.deb` control file
  (Tauri's deb bundler uses a fixed Depends list, not `dpkg-shlibdeps`).
- **No `linux.rpm.depends`** — `rpmbuild` auto-generates `.so` Requires; an
  explicit (distro-varying) name risks an uninstallable rpm. Per spec.
- `.icns` stays in `icon`; the Linux bundler ignores it and uses the PNGs.

- [ ] **Step 3: Verify the edited config still parses and has the new keys**

Run:
```bash
python3 - <<'PY'
import json
c = json.load(open('desktop/src-tauri/tauri.conf.json'))
b = c["bundle"]
assert b["targets"] == "all", b["targets"]
assert b["category"] == "Utility", b.get("category")
assert b["linux"]["deb"]["depends"] == ["libayatana-appindicator3-1"], b["linux"]
assert "rpm" not in b.get("linux", {}), "rpm depends must NOT be set (auto .so Requires)"
assert b["resources"] == {"resources/herdeck-deckapp": "herdeck-deckapp"}, b["resources"]
print("bundle config OK")
PY
```
Expected: `bundle config OK`.

- [ ] **Step 4: Compile the Rust crate — this validates `tauri.conf.json` against Tauri's schema**

Run in a subshell so the command's exit status IS `cargo test`'s (a trailing
`cd ../..` or a pipe to `tail` would mask a failure), and the parent shell stays
at repo root:
```bash
(cd desktop/src-tauri && cargo test)
```
Expected: `test result: ok.` and a zero exit status. If the config is invalid,
`cargo test` exits non-zero with a `generate_context!` / config error.

This is a **real config-validation gate**, not just a regression guard: `lib.rs`
calls `tauri::generate_context!()`, which reads and validates `tauri.conf.json`
against the Tauri config schema **at compile time**. A malformed bundle config or
an invalid `category`/`linux.deb.depends` shape makes the crate fail to compile.
A green `cargo test` therefore proves the edited config is schema-valid (no Rust
source changed, so a failure here means the config is wrong).

- [ ] **Step 5: Commit**

```bash
git add desktop/src-tauri/tauri.conf.json
git commit -m "build(desktop): cross-platform bundle targets + Linux desktop metadata"
```

---

### Task 3: CI Linux build workflow (`release.yml`)

**Files:**
- Create: `.github/workflows/release.yml`

**Interfaces:**
- Consumes: Task 1's host-arch freeze (the Linux runner produces an x86_64
  sidecar) and Task 2's `targets: "all"` (the runner produces appimage+deb+rpm).
- Produces: a `build-linux` job that uploads a `herdeck-linux-x86_64` artifact
  containing the `.AppImage`, `.deb`, and `.rpm`. This job is the verification
  gate for the whole slice.

**Why no unit test:** a CI workflow is declarative YAML; there is no logic to
unit-test. Local verification = the YAML parses and is structurally complete.
The real proof is running the workflow (post-merge, `gh workflow run`), which is
called out in Step 4 and in the plan's final gate — it cannot run on this Mac.

- [ ] **Step 1: Create the workflow file**

Create `.github/workflows/release.yml` with exactly:

```yaml
name: release
on:
  workflow_dispatch:
  push:
    tags: ["v*"]

jobs:
  build-linux:
    name: Build Linux x86_64 (AppImage + deb + rpm)
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4

      - name: Install system dependencies
        run: |
          sudo apt-get update
          sudo apt-get install -y \
            libwebkit2gtk-4.1-dev \
            libgtk-3-dev \
            libayatana-appindicator3-dev \
            librsvg2-dev \
            patchelf \
            rpm \
            build-essential \
            file \
            desktop-file-utils

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Freeze + smoke the sidecar (Linux x86_64)
        run: |
          python -m venv .venv
          .venv/bin/pip install --upgrade pip
          .venv/bin/pip install -e ".[packaging]"
          bash desktop/scripts/build-sidecar.sh
          bash desktop/scripts/smoke-sidecar.sh

      - uses: dtolnay/rust-toolchain@stable

      - name: Cache cargo
        uses: actions/cache@v4
        with:
          path: |
            ~/.cargo/registry
            ~/.cargo/git
            desktop/src-tauri/target
          key: ${{ runner.os }}-cargo-${{ hashFiles('desktop/src-tauri/Cargo.lock') }}

      - uses: actions/setup-node@v4
        with:
          node-version: "20"

      - name: Install frontend deps
        working-directory: desktop
        run: npm ci

      - name: Remove any stale bundle output (warm cache may restore old bundles)
        run: rm -rf desktop/src-tauri/target/release/bundle

      - name: Build Tauri bundles
        working-directory: desktop
        env:
          APPIMAGE_EXTRACT_AND_RUN: "1"
        run: npm run tauri build

      - name: Verify all three Linux bundle formats were produced
        working-directory: desktop/src-tauri/target/release/bundle
        run: |
          set -euo pipefail
          shopt -s nullglob
          fail=0
          for kind in "appimage/*.AppImage" "deb/*.deb" "rpm/*.rpm"; do
            files=( $kind )
            if [ ${#files[@]} -eq 0 ]; then
              echo "FAIL: no file matched $kind"; fail=1
            else
              echo "OK: $kind -> ${files[*]}"
            fi
          done
          [ "$fail" -eq 0 ] || { echo "Missing one or more bundle formats"; exit 1; }

      - name: Inspect rpm runtime Requires (diagnostic — appindicator best-effort)
        working-directory: desktop/src-tauri/target/release/bundle/rpm
        run: |
          for f in *.rpm; do
            echo "=== Requires of $f ==="
            rpm -qpR "$f"
          done

      - name: Upload Linux bundles
        uses: actions/upload-artifact@v4
        with:
          name: herdeck-linux-x86_64
          path: |
            desktop/src-tauri/target/release/bundle/appimage/*.AppImage
            desktop/src-tauri/target/release/bundle/deb/*.deb
            desktop/src-tauri/target/release/bundle/rpm/*.rpm
          if-no-files-found: error
```

Notes for the implementer (do not add to the file):
- `HERDECK_PY` defaults to `$ROOT/.venv/bin/python` inside `build-sidecar.sh`, so
  creating the venv at repo-root `.venv` wires the freeze to it automatically.
- `smoke-sidecar.sh` uses `python3` (provided by setup-python) for JSON parsing
  and the venv python (with Pillow from `.[packaging]`) to decode the baked glyph.
- `APPIMAGE_EXTRACT_AND_RUN=1` avoids the FUSE requirement on the runner.
- The **"Remove any stale bundle output"** step deletes
  `desktop/src-tauri/target/release/bundle` before the build. The cargo cache
  includes `target`, so a warm-cache run could otherwise restore OLD
  AppImage/deb/rpm files; if a format then failed to rebuild, the verify step
  would falsely pass on the stale artifact (and a stale file could be uploaded).
  Cleaning first guarantees verify + upload only see freshly-built artifacts.
- The **"Verify all three Linux bundle formats"** step is the real completeness
  gate: `if-no-files-found: error` only fails when *zero* paths match, so it would
  still pass if e.g. the rpm target silently produced nothing while appimage+deb
  exist. The explicit per-format glob check fails the job if any one is missing.
- The **rpm `rpm -qpR` diagnostic** logs the rpm's runtime `Requires`. The tray's
  appindicator lib is loaded dynamically (Tauri's `TRAY_LIBRARY_PATH`), so it may
  NOT appear as an ELF `NEEDED` entry and rpmbuild's auto `.so` requirement
  generation can miss it. v1 treats rpm tray-dep completeness as **best-effort**
  (the AppImage bundles appindicator; the deb declares it explicitly). The
  diagnostic makes the rpm's actual Requires visible in the CI log for a human to
  judge; it does not fail the build.
- `if-no-files-found: error` stays as a secondary guard on the upload itself.
- The `rpm` apt package supplies `rpmbuild`, required for the rpm target.
- No GitHub Release step in v1: `workflow_dispatch` just uploads artifacts. (A
  release-attach step on tags is a documented future add; not in scope here.)

- [ ] **Step 2: Verify the workflow YAML parses and is structurally complete**

Run:
```bash
python3 - <<'PY'
import yaml
w = yaml.safe_load(open('.github/workflows/release.yml'))
# PyYAML parses the bare `on:` key as boolean True — assert against that.
assert True in w or "on" in w, "missing trigger block"
trig = w.get(True, w.get("on"))
assert "workflow_dispatch" in trig, trig
assert trig["push"]["tags"] == ["v*"], trig["push"]
job = w["jobs"]["build-linux"]
assert job["runs-on"] == "ubuntu-24.04", job["runs-on"]
steps = job["steps"]
joined = "\n".join(str(s) for s in steps)
for needle in ["libwebkit2gtk-4.1-dev", "libayatana-appindicator3-dev", "rpm",
               "build-sidecar.sh", "smoke-sidecar.sh", "npm run tauri build",
               "APPIMAGE_EXTRACT_AND_RUN", "if-no-files-found",
               "Remove any stale bundle output", "Verify all three", "rpm -qpR"]:
    assert needle in joined, f"missing step content: {needle}"
print("workflow OK")
PY
```
Expected: `workflow OK`. (If PyYAML is not installed, run
`.venv/bin/pip install pyyaml` first, or use `.venv/bin/python` which has it via
deps — PyYAML ships transitively; if absent, `pip install pyyaml` in the venv.)

- [ ] **Step 3: Confirm `ci.yml` is unchanged (the fast test CI must not gain the slow build)**

Run:
```bash
git diff --name-only HEAD -- .github/workflows/ci.yml
```
Expected: empty output (no changes to `ci.yml`).

- [ ] **Step 4: Document how the Linux build is actually verified (no local run possible)**

This step produces no code — it records the gate. The Linux build CANNOT run on
this Mac. After this branch is merged to `main` and pushed (with user approval),
verify with:
```bash
gh workflow run release.yml
# then watch it:
gh run list --workflow=release.yml --limit 1
gh run watch <run-id>
```
Expected: the `build-linux` job is green and the `herdeck-linux-x86_64` artifact
contains a `.AppImage`, a `.deb`, and a `.rpm`. (Before merge, the same can be
triggered by pushing a throwaway pre-release tag, e.g.
`git tag v0.0.0-rc.1 && git push origin v0.0.0-rc.1`, then deleting it.)

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/release.yml
git commit -m "ci: Linux x86_64 release build (AppImage + deb + rpm)"
```

---

## Final gate (after all tasks)

1. **macOS no-regression (local, runnable now):**
   `bash desktop/scripts/build-sidecar.sh && bash desktop/scripts/smoke-sidecar.sh`
   → `SMOKE PASS` (host=arm64 freeze intact).
2. **Existing suites green:** `.venv/bin/python -m pytest -q` and
   `cd desktop/src-tauri && cargo test` and `cd desktop && npm test` — all pass
   (these changes touch none of their code).
3. **Linux build (CI, post-merge):** `gh workflow run release.yml` → `build-linux`
   green; `herdeck-linux-x86_64` artifact has `.AppImage` + `.deb` + `.rpm`. This
   is the slice's defining proof and is the only place the Linux build exists.

## Notes / deferred (documented, not in scope)

- **arm64 Linux** — deferred (private-repo arm64 hosted runners are paid; arm64
  freeze needs an arm64 host or slow QEMU). Add an `ubuntu-24.04-arm` job (or
  QEMU) when a paid plan / runner is available.
- **GitHub Release attach** on tags — future add (`softprops/action-gh-release`).
- **Wayland global hotkey** — unsupported by Wayland design; deck still works.
