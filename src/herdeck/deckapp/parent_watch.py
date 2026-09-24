"""Tie a shell-spawned runtime's lifetime to the desktop app that spawned it.

The desktop shell kills its sidecar on a normal quit, but a crash, SIGKILL or
Force Quit never runs that path: the orphaned runtime used to keep serving —
and keep holding the D200 — until the next reboot. The shell therefore spawns
the sidecar with ``HERDECK_PARENT_WATCH=1`` and a piped stdin it never writes
to and never closes while alive. The kernel closes that pipe when the shell
dies for ANY reason, so stdin EOF is the primary "parent is gone" signal. A
``getppid()`` change (re-parented to launchd/init) is polled as a fallback for
the case where stdin was somehow not a pipe.

Only an explicit opt-in arms the watch: a runtime started from a terminal, by
launchd, or by a smoke script with ``</dev/null`` must never exit on stdin EOF.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

PARENT_WATCH_ENV = "HERDECK_PARENT_WATCH"


def parent_watch_enabled(environ=None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(PARENT_WATCH_ENV) == "1"


def _drain_until_eof(stream) -> None:
    try:
        while True:
            if not stream.read(4096):
                return
    except (OSError, ValueError):
        return  # a closed/invalid stdin is as good as EOF


def watch_parent(
    stop: threading.Event,
    *,
    stdin=None,
    getppid: Callable[[], int] = os.getppid,
    poll_interval: float = 1.0,
) -> list[threading.Thread]:
    """Set ``stop`` once the spawning parent is gone (stdin EOF or re-parent).

    ``stop`` is the same event SIGTERM sets, so the caller's normal shutdown
    path (close sinks, remove runtime files) runs unchanged. Returns the daemon
    threads (for tests); neither blocks process exit.
    """
    stream = stdin if stdin is not None else getattr(sys.stdin, "buffer", sys.stdin)
    original_ppid = getppid()

    def on_eof() -> None:
        _drain_until_eof(stream)
        if not stop.is_set():
            log.warning("parent shell closed stdin; shutting the runtime down")
            stop.set()

    def on_reparent() -> None:
        while not stop.wait(poll_interval):
            ppid = getppid()
            if ppid != original_ppid or ppid == 1:
                log.warning(
                    "parent shell exited (ppid %s -> %s); shutting the runtime down",
                    original_ppid,
                    ppid,
                )
                stop.set()
                return

    threads = [
        threading.Thread(target=on_eof, name="herdeck-parent-stdin", daemon=True),
        threading.Thread(target=on_reparent, name="herdeck-parent-ppid", daemon=True),
    ]
    for thread in threads:
        thread.start()
    return threads
