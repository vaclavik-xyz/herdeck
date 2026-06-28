#!/usr/bin/env bash
# Headless smoke of the FROZEN deckapp sidecar: spawn it, read its discovery line,
# and assert it serves the token-authed loopback API (mock source — no config).
# Usage: bash desktop/scripts/smoke-sidecar.sh [path/to/herdeck-deckapp]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP="$(dirname "$HERE")"
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
python3 - "$ASSETS_DIR" <<'PY'
import hashlib, os, sys
d = sys.argv[1]
svg_path = os.path.join(d, "codex.svg")
assert os.path.exists(svg_path), f"FAIL: codex.svg not bundled in {d}"
svg = open(svg_path, encoding="utf-8").read()
name = hashlib.sha1(svg.encode("utf-8")).hexdigest() + ".png"
png = os.path.join(d, name)
assert os.path.exists(png), f"FAIL: baked codex PNG {name} missing from bundle (frozen rasterizer would fall back to a letter)"
print("OK: baked codex PNG present:", name)
PY

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
