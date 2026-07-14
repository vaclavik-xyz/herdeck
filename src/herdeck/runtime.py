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

from .deckapp.discovery import clear_runtime_file, runtime_file_path, write_runtime_file
from .deckapp.server import create_app
from .deckapp.sinks import ReconnectingD200Sink


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


def main() -> int:
    if os.environ.get("HERDECK_DEBUG"):
        logging.basicConfig(level=logging.DEBUG)
    port = int(os.environ.get("HERDECK_DECKAPP_PORT", "0"))
    app, sink, info, path = build_runtime(host="127.0.0.1", port=port)
    print(json.dumps(info), flush=True)  # stdout discovery fallback (parity with the sidecar)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        stop.wait()
    finally:
        clear_runtime_file(path)
        if sink is not None:
            sink.close()
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
