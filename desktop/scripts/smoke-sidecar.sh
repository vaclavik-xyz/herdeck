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
if [ -x "$PY" ] && "$PY" -c "import PIL" 2>/dev/null; then
  # Strongest proof: decode the baked PNG exactly as the frozen rasterizer does.
  "$PY" - "$ASSETS_DIR" <<'PY'
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
else
  # Fallback (no Pillow available): stdlib PNG signature + per-chunk CRC validation.
  python3 - "$ASSETS_DIR" <<'PY'
import hashlib, os, sys, zlib
d = sys.argv[1]
svg_path = os.path.join(d, "codex.svg")
assert os.path.exists(svg_path), f"FAIL: codex.svg not bundled in {d}"
svg = open(svg_path, encoding="utf-8").read()
name = hashlib.sha1(svg.encode("utf-8")).hexdigest() + ".png"
png = os.path.join(d, name)
assert os.path.exists(png), f"FAIL: baked codex PNG {name} missing from bundle"
data = open(png, "rb").read()
assert data[:8] == b"\x89PNG\r\n\x1a\n", f"FAIL: baked codex PNG not a PNG: {png}"
# Walk every chunk, verifying declared length + CRC32, so a corrupt/truncated
# IDAT (or any chunk) is caught — proving the PNG is structurally decodable.
pos, first, saw_iend, width, height = 8, True, False, None, None
while pos < len(data):
    assert pos + 8 <= len(data), f"FAIL: truncated chunk header in {png}"
    length = int.from_bytes(data[pos:pos+4], "big")
    ctype = data[pos+4:pos+8]
    cdata = data[pos+8:pos+8+length]
    assert len(cdata) == length, f"FAIL: truncated chunk {ctype!r} in {png}"
    stored = data[pos+8+length:pos+12+length]
    assert len(stored) == 4, f"FAIL: missing CRC for {ctype!r} in {png}"
    assert (zlib.crc32(ctype + cdata) & 0xffffffff) == int.from_bytes(stored, "big"), \
        f"FAIL: bad CRC for chunk {ctype!r} in {png}"
    if first:
        assert ctype == b"IHDR", f"FAIL: first chunk not IHDR in {png}"
        width = int.from_bytes(cdata[0:4], "big")
        height = int.from_bytes(cdata[4:8], "big")
        first = False
    if ctype == b"IEND":
        saw_iend = True
        break
    pos += 12 + length
assert width == 196 and height == 196, f"FAIL: baked codex PNG dims {width}x{height}, want 196x196: {png}"
assert saw_iend, f"FAIL: baked codex PNG missing IEND chunk: {png}"
print(f"OK: baked codex PNG valid (196x196, CRC-checked): {name}")
PY
fi

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
