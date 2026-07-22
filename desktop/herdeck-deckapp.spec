# PyInstaller spec — host-arch onedir frozen herdeck.deckapp sidecar for the desktop app.
# Build via desktop/scripts/build-sidecar.sh, e.g.:
#   pyinstaller desktop/herdeck-deckapp.spec --noconfirm \
#     --distpath desktop/src-tauri/resources --workpath build/pyinstaller-deckapp
# COLLECT(name="herdeck-deckapp") itself creates the herdeck-deckapp/ folder UNDER
# --distpath, so the exe lands at <distpath>/herdeck-deckapp/herdeck-deckapp.
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # repo root (SPECPATH = desktop/)
CODESIGN_IDENTITY = os.environ.get("APPLE_SIGNING_IDENTITY") or None

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
        "herdeck.deckapp.onboarding",
        "herdeck.deckapp.local_bridge",
        "herdeck.deckapp.probe",
        "herdeck.bridge",
        "herdeck.bootstrap",
        "herdeck.connector",
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
    # PyInstaller otherwise ad-hoc signs its executable and every collected
    # Mach-O dependency. Release builds provide Developer ID so the complete
    # nested sidecar satisfies Apple's notarization requirements.
    codesign_identity=CODESIGN_IDENTITY,
    # No target_arch -> PyInstaller freezes for the HOST arch: arm64 on the dev
    # Mac, x86_64 on the Linux CI runner. One spec serves both OSes (3e).
)
coll = COLLECT(exe, a.binaries, a.datas, name="herdeck-deckapp")
