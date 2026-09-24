"""PyInstaller entry for the frozen converged Herdeck runtime.

The production desktop bundle starts the same loopback API as the development
sidecar and also supervises the physical Ulanzi D200. Both paths emit the same
discovery JSON contract, so the Rust shell does not need a packaging-specific
transport.

``herdeck-deckapp service ...`` instead runs the herdeck-service CLI
(herdeck.service.main), so the app can install, restart and query its runtime
service without a Python install, e.g.
``<bundle>/Contents/Resources/herdeck-deckapp/herdeck-deckapp service install
runtime --from-app``.
"""

import sys


def _dispatch(argv: list[str]) -> int:
    if argv[:1] == ["service"]:
        from herdeck.service import main as service_main

        service_main(argv[1:])
        return 0
    from herdeck.runtime import main

    return main()


if __name__ == "__main__":
    sys.exit(_dispatch(sys.argv[1:]))
