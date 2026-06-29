"""LocalBridgeRunner — owns the embedded herdr bridge on its own asyncio loop.

The bridge (start_local_bridge: a loopback WebSocket server reading the herdr Unix
socket) and the deckapp's Connector run on two SEPARATE loops/threads, talking only
over the loopback WebSocket. This runner mirrors live.ConnectorRunner: start the
bridge, block until bound, keep the loop running to serve it, and tear it down on
close()."""
from __future__ import annotations

import asyncio
import threading

from ..bridge import start_local_bridge


class LocalBridgeRunner:
    def __init__(self, socket_path: str, *, start_bridge=start_local_bridge):
        self._socket_path = socket_path
        self._start_bridge = start_bridge
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._serve, name="herdeck-local-bridge", daemon=True
        )
        self._ready = threading.Event()
        self._bound: tuple[str, int, str] | None = None
        self._handle = None  # (server, btask)
        self._error: BaseException | None = None

    def start(self) -> tuple[str, int, str]:
        self._thread.start()
        self._ready.wait(timeout=10)
        if self._error is not None:
            raise self._error
        if self._bound is None:
            raise RuntimeError("local bridge did not bind within 10s")
        return self._bound

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            host, port, token, handle = self._loop.run_until_complete(
                self._start_bridge(self._socket_path)
            )
            self._bound = (host, port, token)
            self._handle = handle
        except BaseException as exc:  # surface to start()
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()  # keep serving the bound bridge + broadcast task

    def close(self) -> None:
        loop = self._loop
        if loop.is_closed():
            return
        handle = self._handle

        async def _shutdown():
            if handle is not None:
                server, btask = handle
                btask.cancel()
                try:
                    await btask
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                server.close()
                try:
                    await server.wait_closed()
                except Exception:
                    pass

        try:
            asyncio.run_coroutine_threadsafe(_shutdown(), loop).result(timeout=2)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        if self._thread.is_alive():
            self._thread.join(timeout=2)
        try:
            loop.close()
        except Exception:
            pass
