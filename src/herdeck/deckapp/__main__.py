"""CLI entry: run the sidecar and print its loopback URL + access token.

The desktop shell spawns this, reads one JSON line from stdout (the discovery
contract — host, port, and the one-time access token), and hands them to the
WebView. This is the ONLY place the access token is emitted; it is never written
to a file, never in request logs, and never in /state or /health.

The source is chosen at startup: a LiveSource (real bridge via Connector) when a
server + bridge token are configured, otherwise the deterministic MockSource. The
bridge token never appears in the discovery line — only the loopback access token
(``token``) and the chosen source name do.
"""

from __future__ import annotations

import json
import os
import sys
import threading

from .server import create_live_app, create_mock_app, select_live


def main() -> int:
    port = int(os.environ.get("HERDECK_DECKAPP_PORT", "0"))
    # Bind to the IPv4 loopback only (per spec). We deliberately do NOT honour a
    # configurable bind here: anything else (`localhost`, `::1`, a LAN address)
    # would either widen exposure or mismatch the IPv4 server / bracket-less URL.
    host = "127.0.0.1"

    selected = select_live()
    if selected is None:
        app = create_mock_app(host=host, port=port)
        source = "mock"
    else:
        config, server = selected
        app = create_live_app(config, server, host=host, port=port)
        source = "live"
    discovery = {
        "url": f"http://{app.host}:{app.port}",
        "host": app.host,
        "port": app.port,
        "token": app.token,
        "source": source,
    }
    print(json.dumps(discovery), flush=True)

    stop = threading.Event()
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
