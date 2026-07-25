#!/usr/bin/env bash
# Verify every Mach-O file in the frozen PyInstaller sidecar is release-signed.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "SKIP: macOS sidecar signing verification"
  exit 0
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP="$(dirname "$HERE")"
BUNDLE="${1:-$DESKTOP/src-tauri/resources/herdeck-deckapp}"
EXPECTED_IDENTITY="${APPLE_SIGNING_IDENTITY:?APPLE_SIGNING_IDENTITY is required}"
EXPECTED_TEAM="${APPLE_TEAM_ID:?APPLE_TEAM_ID is required}"

test -d "$BUNDLE" || { echo "FAIL: sidecar bundle not found at $BUNDLE"; exit 1; }

count=0
while IFS= read -r -d '' candidate; do
  /usr/bin/file -b "$candidate" | grep -q "Mach-O" || continue
  count=$((count + 1))

  details="$(/usr/bin/codesign -dv --verbose=4 "$candidate" 2>&1)" || {
    echo "FAIL: unsigned Mach-O file: $candidate"
    exit 1
  }
  grep -Fqx "Authority=$EXPECTED_IDENTITY" <<<"$details" || {
    echo "FAIL: wrong signing identity: $candidate"
    exit 1
  }
  grep -Fqx "TeamIdentifier=$EXPECTED_TEAM" <<<"$details" || {
    echo "FAIL: wrong signing team: $candidate"
    exit 1
  }
  grep -Eq '^CodeDirectory .*flags=.*\(runtime\)' <<<"$details" || {
    echo "FAIL: hardened runtime missing: $candidate"
    exit 1
  }
  grep -q '^Timestamp=' <<<"$details" || {
    echo "FAIL: secure timestamp missing: $candidate"
    exit 1
  }
  /usr/bin/codesign --verify --strict "$candidate"
done < <(/usr/bin/find "$BUNDLE" -type f -print0)

(( count > 0 )) || { echo "FAIL: no Mach-O files found in $BUNDLE"; exit 1; }
echo "OK: $count Mach-O sidecar files have Developer ID, hardened runtime, and timestamps"
