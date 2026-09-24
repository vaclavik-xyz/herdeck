"""PyInstaller entry: launch the herdeck backend exactly like the console script.

``HERDECK_SELFTEST=imports`` instead proves the frozen import graph loads (CI
runs it on the frozen binary) and exits without starting the backend.
"""
import os

if __name__ == "__main__":
    if os.environ.get("HERDECK_SELFTEST") == "imports":
        from herdeck.elgato.runtime import run_import_selftest

        raise SystemExit(run_import_selftest())
    from herdeck.host import main

    main()
