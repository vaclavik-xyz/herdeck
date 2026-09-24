"""Headless converged herdeck runtime.

One process owns one Orchestrator + one herdr-bridge connection (via the
deckapp LiveSource) + one tick loop + one clock, and fans render frames out to
sinks: the HTTP tile buffer (served to the desktop window / web) and — when a
physical Ulanzi D200 is attached — a D200 USB sink. It publishes its localhost
address in runtime.json so the desktop window can attach instead of spawning
its own sidecar. Run as the launchd service on a machine with a D200; on a
machine without one it is simply an HTTP-only deck server."""

from __future__ import annotations

import json
import logging
import os
import signal
import threading

from .deckapp.device_lock import DeviceLock, d200_lock_path
from .deckapp.discovery import clear_runtime_file, runtime_file_path, write_runtime_file
from .deckapp.parent_watch import parent_watch_enabled, watch_parent
from .deckapp.server import create_app
from .deckapp.sinks import ReconnectingD200Sink

SELFTEST_IMPORTS = (
    "herdeck.deckapp.onboarding",
    "herdeck.deckapp.local_bridge",
    "herdeck.bridge",
    "herdeck.runtime",
    "herdeck.driver.d200",
    "strmdck",
    "strmdck.devices.ulanzi_d200",
    "hid",
    "resvg_py",
)


def _run_import_selftest() -> int:
    import importlib

    for module in SELFTEST_IMPORTS:
        importlib.import_module(module)
    # The native resvg module must also load and render inside the bundle
    # (it draws SVG project favicons there).
    from .icons import resvg_rasterize

    resvg_rasterize('<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>', 8)
    return 0


def _should_write_discovery() -> bool:
    """Only standalone runtimes own the shared runtime.json discovery file."""
    return os.environ.get("HERDECK_RUNTIME_MANAGED") != "1"


def _default_driver_factory(config):
    """Build (and open) a D200Driver from config.hardware. Raises if no device."""
    from .driver.d200 import D200Driver

    hw = config.hardware
    return D200Driver(
        brightness=hw.brightness,
        debounce=hw.debounce,
        keep_alive_interval=hw.keep_alive_interval,
        icons_dir=hw.icons_dir,
    )


def _build_d200_sink(app, *, driver_factory):
    """Attach a persistent D200 sink that survives USB loss and Mac sleep."""
    sink = ReconnectingD200Sink(
        lambda: driver_factory(app.config),
        on_press=app.press,
        slots=app.slots,
        # One D200 owner per machine: a second runtime (launchd + an app's own
        # sidecar) serves its window but leaves the device alone.
        device_lock=DeviceLock(d200_lock_path()),
    )
    app.add_sink(sink)
    return sink


def build_runtime(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    app_factory=None,
    driver_factory=None,
    write_discovery: bool = True,
):
    """Build the serving deck app, supervise its D200, and publish runtime.json.

    Returns ``(app, reconnecting_sink, info, path)``. The sink remains present
    in HTTP-only mode and keeps probing until a D200 becomes available.
    """
    app_factory = app_factory or (lambda host, port: create_app(host=host, port=port))
    driver_factory = driver_factory or _default_driver_factory
    app = app_factory(host, port)
    sink = _build_d200_sink(app, driver_factory=driver_factory)
    info = {
        "url": f"http://{app.host}:{app.port}",
        "host": app.host,
        "port": app.port,
        "token": app.token,
        "source": app.source_name,
    }
    path = runtime_file_path()
    if write_discovery:
        write_runtime_file(path, info)
    return app, sink, info, path


def configure_logging(*, debug: bool) -> None:
    """Warnings for everything; each notification's route (queued / osascript
    fallback) at INFO too — the desktop app keeps stderr in its log file, and
    those lines are what explain a banner that arrived the wrong way."""
    if debug:
        logging.basicConfig(level=logging.DEBUG)
        return
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    for name in ("herdeck.notify", "herdeck.deckapp.live"):  # INFO = notification lines only
        logging.getLogger(name).setLevel(logging.INFO)


def main() -> int:
    if os.environ.get("HERDECK_SELFTEST") == "imports":
        return _run_import_selftest()
    configure_logging(debug=bool(os.environ.get("HERDECK_DEBUG")))
    port = int(os.environ.get("HERDECK_DECKAPP_PORT", "0"))
    write_discovery = _should_write_discovery()
    app, sink, info, path = build_runtime(
        host="127.0.0.1",
        port=port,
        write_discovery=write_discovery,
    )
    print(json.dumps(info), flush=True)  # stdout discovery fallback (parity with the sidecar)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if parent_watch_enabled():
        # Spawned by the desktop shell: exit through this same clean path when
        # the shell dies (crash / SIGKILL / Force Quit), not just on SIGTERM.
        watch_parent(stop)
    try:
        stop.wait()
    finally:
        if write_discovery:
            clear_runtime_file(path)
        if sink is not None:
            sink.close()
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
