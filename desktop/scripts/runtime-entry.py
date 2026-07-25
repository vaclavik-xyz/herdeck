"""PyInstaller entry for the frozen converged Herdeck runtime.

The production desktop bundle starts the same loopback API as the development
sidecar and also supervises the physical Ulanzi D200. Both paths emit the same
discovery JSON contract, so the Rust shell does not need a packaging-specific
transport.
"""

import sys

from herdeck.runtime import main

if __name__ == "__main__":
    sys.exit(main())
