"""PyInstaller entry for the frozen herdeck.deckapp sidecar.

PyInstaller analyses a real script file (not a ``-m`` target), so this thin
wrapper just delegates to the existing module main, which prints the discovery
JSON line and serves the loopback HTTP API.
"""

import sys

from herdeck.deckapp.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
