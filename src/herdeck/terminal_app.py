"""Bring the terminal app forward after a deck tile press focused a pane.

herdr's ``agent.focus`` only switches the pane inside the herdr client; when
that client is a window on the deck machine (e.g. Ghostty running ``herdr``),
the window itself may sit behind other apps. ``[local].terminal_app`` names
the app to activate afterwards (``open -a <name>``). It is device-local and
only meaningful when the herdr client is attached on the machine that runs the
deck — activating a local terminal does nothing for a pane shown on another
host. macOS only; empty (the default) turns it off.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading

log = logging.getLogger("herdeck.terminal_app")

_OPEN_TIMEOUT_S = 5.0


def _run_open(name: str, run) -> None:
    try:
        result = run(
            ["open", "-a", name],
            check=False,
            capture_output=True,
            timeout=_OPEN_TIMEOUT_S,
        )
    except Exception:  # never let a missing app / hung LaunchServices escape
        log.warning("could not activate terminal app %r", name, exc_info=True)
        return
    if getattr(result, "returncode", 0) != 0:
        log.warning("open -a %r failed (exit %s)", name, result.returncode)


def activate_terminal_app(
    name: str | None,
    *,
    platform: str | None = None,
    run=subprocess.run,
    spawn=None,
) -> bool:
    """Activate ``name`` off the calling thread; True when an attempt started.

    Never raises and never blocks the caller (a connector loop or the deck
    lock): the ``open`` call runs on a daemon thread. ``spawn`` replaces that
    thread in tests."""
    name = (name or "").strip()
    if not name or (platform or sys.platform) != "darwin":
        return False
    if spawn is None:

        def spawn(fn):
            threading.Thread(target=fn, name="herdeck-terminal-app", daemon=True).start()

    try:
        spawn(lambda: _run_open(name, run))
    except Exception:
        log.warning("could not schedule terminal app activation", exc_info=True)
        return False
    return True
