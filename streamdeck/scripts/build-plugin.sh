#!/usr/bin/env bash
# Local, unsigned, arm64 build of the herdeck .streamDeckPlugin (frozen backend bundled).
# Reproducible in a clean env after: pip install -e .[packaging]  (PyInstaller + cairosvg + deps)
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
