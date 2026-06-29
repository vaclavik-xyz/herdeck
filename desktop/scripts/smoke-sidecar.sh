#!/usr/bin/env bash
# Headless smoke of the FROZEN deckapp sidecar: spawn it, read its discovery line,
# and assert it serves the token-authed loopback API (mock source — no config).
# Usage: bash desktop/scripts/smoke-sidecar.sh [path/to/herdeck-deckapp]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP="$(dirname "$HERE")"
ROOT="$(dirname "$DESKTOP")"
PY="${HERDECK_PY:-$ROOT/.venv/bin/python}"
BIN="${1:-$DESKTOP/src-tauri/resources/herdeck-deckapp/herdeck-deckapp}"
test -x "$BIN" || { echo "FAIL: frozen binary not found at $BIN"; exit 1; }

# --- Frozen baked-asset proof (robust; no tile-index assumptions) ---------------
# The ONLY bundled SVG glyph is codex.svg, so a 200 from /tile is NOT proof the
# frozen PNG rasterizer ran (a broken/missing baked PNG silently degrades to a
# letter glyph and still returns 200). Instead assert the baked codex PNG is in the
# bundle: that proves prerasterize + bundling worked and the frozen rasterizer will
# find it. (The Task 2 unit test already proves the frozen wiring loads it.)
BINDIR="$(dirname "$BIN")"
ASSETS_DIR=""
for cand in "$BINDIR/_internal/herdeck_assets" "$BINDIR/herdeck_assets"; do
  [ -d "$cand" ] && ASSETS_DIR="$cand" && break
done
[ -n "$ASSETS_DIR" ] || { echo "FAIL: bundled herdeck_assets dir not found under $BINDIR"; exit 1; }
# This gate REQUIRES a real decode of the baked glyph — it never accepts a weaker
# (CRC-only) check. Prefer the build venv python, then system python3.
DECODER=""
for cand in "$PY" python3; do
  if [ -n "$cand" ] && command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import PIL" 2>/dev/null; then
    DECODER="$cand"; break
  fi
done
[ -n "$DECODER" ] || { echo "FAIL: no Pillow-capable interpreter (tried '$PY' and python3) to decode the baked glyph"; exit 1; }
"$DECODER" - "$ASSETS_DIR" <<'PY'
import hashlib, os, sys
from PIL import Image
d = sys.argv[1]
svg_path = os.path.join(d, "codex.svg")
assert os.path.exists(svg_path), f"FAIL: codex.svg not bundled in {d}"
svg = open(svg_path, encoding="utf-8").read()
name = hashlib.sha1(svg.encode("utf-8")).hexdigest() + ".png"
png = os.path.join(d, name)
assert os.path.exists(png), f"FAIL: baked codex PNG {name} missing from bundle"
im = Image.open(png)
im.load()                  # forces full IDAT inflate + decode (raises on corrupt data)
im = im.convert("RGBA")    # the exact op herdeck.frozen.make_png_rasterizer performs
assert im.size == (196, 196), f"FAIL: baked codex PNG dims {im.size}, want (196, 196): {png}"
print(f"OK: baked codex PNG decodes (196x196 RGBA): {name}")
PY

# --- Frozen local-bridge import reachability ---------------------------------------
# The local onboarding path pulls herdeck.bridge/bootstrap + the new deckapp modules.
# A full local connect needs a herdr socket (unit-tested via StubHerdr), but assert
# here that the FROZEN binary can import the whole local path (no missing hiddenimport).
# Self-contained temp file (defined + cleaned up here) so it is safe under `set -u`,
# regardless of where it sits relative to the other mktemp lines.
IMPORT_ERR="$(mktemp)"
if ! HERDECK_SELFTEST=imports "$BIN" >/dev/null 2>"$IMPORT_ERR"; then
  echo "FAIL: frozen local-bridge imports unreachable"; cat "$IMPORT_ERR"; rm -f "$IMPORT_ERR"; exit 1
fi
rm -f "$IMPORT_ERR"
echo "OK: frozen local-bridge imports reachable"

# Force the deterministic mock source (no on-disk config / keychain needed).
export HERDECK_MOCK=1

LINE_FILE="$(mktemp)"
STDERR_FILE="$(mktemp)"
"$BIN" >"$LINE_FILE" 2>"$STDERR_FILE" &
PID=$!
trap 'kill "$PID" 2>/dev/null || true; rm -f "$STDERR_FILE"' EXIT

# Wait up to ~30s for the discovery line.
for _ in $(seq 1 300); do
  [ -s "$LINE_FILE" ] && break
  sleep 0.1
done
DISCOVERY="$(head -n1 "$LINE_FILE")"
[ -n "$DISCOVERY" ] || { echo "FAIL: no discovery line"; echo "--- sidecar stderr: ---"; cat "$STDERR_FILE"; exit 1; }
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
