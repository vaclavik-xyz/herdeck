# PyInstaller spec — arm64 onedir frozen herdeck backend for the Elgato plugin.
# Build via streamdeck/scripts/build-plugin.sh, e.g.:
#   pyinstaller streamdeck/herdeck-backend.spec --noconfirm \
#     --distpath …/xyz.vaclavik.herdeck.sdPlugin/backend --workpath build/pyinstaller
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # repo root (SPECPATH = streamdeck/)

a = Analysis(
    [os.path.join(SPECPATH, "scripts", "herdeck-backend-entry.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=[],
    datas=[(os.path.join(ROOT, "src", "herdeck", "assets"), "herdeck_assets")],
    # The elgato submodules serve_elgato reaches, plus websockets pinned explicitly. websockets
    # is a CORE dep: connector.py imports it at module top level (ELGATO uses Connector to reach
    # each server via websockets.connect), so the frozen backend needs it — it auto-bundles via
    # that import, but is listed here as a safety net. Add more ONLY if the manual smoke run
    # shows PyInstaller missed a real serve_elgato-graph import.
    hiddenimports=[
        "herdeck.elgato.runtime",
        "herdeck.elgato.frozen",
        "herdeck.elgato.session",
        "herdeck.elgato.ipc",
        "websockets",
    ],
    # cairosvg (+ its native cffi/cairocffi chain) is build-time only — the frozen session uses
    # the Pillow PNG rasterizer. Also drop the native HID driver stack (StreamDeck + hid →
    # hidapi/libusb) that only the lazy, unreached make_deck importers pull, so the bundle stays
    # slim. Do NOT drop websockets (core Connector dep). pyserial/serial is not a repo dep.
    # NEVER list anything in the serve_elgato import graph here.
    excludes=["cairosvg", "cffi", "cairocffi", "tkinter", "StreamDeck", "hid"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="herdeck-backend",
    console=True,
    target_arch="arm64",
)
coll = COLLECT(exe, a.binaries, a.datas, name="herdeck-backend")
