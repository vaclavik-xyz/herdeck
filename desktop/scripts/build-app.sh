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
