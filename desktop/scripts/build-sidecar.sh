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
