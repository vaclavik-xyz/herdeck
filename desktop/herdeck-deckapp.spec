# PyInstaller spec — host-arch onedir frozen converged runtime for the desktop app.
# Build via desktop/scripts/build-sidecar.sh, e.g.:
#   pyinstaller desktop/herdeck-deckapp.spec --noconfirm \
#     --distpath desktop/src-tauri/resources --workpath build/pyinstaller-deckapp
# COLLECT(name="herdeck-deckapp") itself creates the herdeck-deckapp/ folder UNDER
# --distpath, so the exe lands at <distpath>/herdeck-deckapp/herdeck-deckapp.
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # repo root (SPECPATH = desktop/)
CODESIGN_IDENTITY = os.environ.get("APPLE_SIGNING_IDENTITY") or None

a = Analysis(
    [os.path.join(SPECPATH, "scripts", "runtime-entry.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=[],
    # Bundle the assets dir (SVG glyphs + the pre-baked PNGs the baker writes into
    # it) as herdeck_assets. baked_assets_dir() resolves to it via sys._MEIPASS.
    datas=[(os.path.join(ROOT, "src", "herdeck", "assets"), "herdeck_assets")],
    # Runtime graph: source/live/mock + WS bridge + the dynamically imported D200
    # driver stack. websockets is a CORE dep. tomli_w is imported at the top of
    # deckapp.config_service; listed as a safety net against the lazy path.
    hiddenimports=[
        "herdeck.runtime",
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
        "herdeck.driver.d200",
        "strmdck",
        "strmdck.devices.ulanzi_d200",
        "hid",
        "websockets",
        "tomli_w",
    ],
    # cairosvg (+ native cffi/cairocffi) is build-time only — the frozen runtime
    # uses the Pillow PNG rasterizer. The Elgato StreamDeck package is not part of
    # this D200-first desktop installer. Never exclude strmdck, hid, or websockets.
    excludes=["cairosvg", "cffi", "cairocffi", "tkinter", "StreamDeck"],
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
