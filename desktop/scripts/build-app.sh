#!/usr/bin/env bash
# Build the full herdeck desktop app: freeze the sidecar, then run the Tauri GUI build.
# This needs a desktop session + the GUI toolchain (it does NOT run headless).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # desktop/scripts
DESKTOP="$(dirname "$HERE")"                            # desktop

bash "$HERE/build-sidecar.sh"

echo "==> tauri build (native bundles for the host OS)"
cd "$DESKTOP"
npm run tauri build
