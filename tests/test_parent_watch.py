"""A shell-spawned runtime must die with the desktop shell (crash/SIGKILL too)."""

import os
import subprocess
import sys
import threading
import time

from herdeck import runtime
from herdeck.deckapp import parent_watch

CHILD = """
import threading
from herdeck.deckapp.parent_watch import watch_parent
stop = threading.Event()
watch_parent(stop, poll_interval=30)
print("ready", flush=True)
stop.wait()
print("stopped", flush=True)
"""


def _child(env_extra=None):
    env = {**os.environ, **(env_extra or {})}
    return subprocess.Popen(
        [sys.executable, "-c", CHILD],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
    )


def test_child_exits_cleanly_when_the_parent_pipe_closes():
    proc = _child()
    try:
        assert proc.stdout.readline().strip() == "ready"
        assert proc.poll() is None  # an open pipe keeps it alive
        proc.stdin.close()  # what the kernel does when the shell dies
        out, _ = proc.communicate(timeout=5)
        assert out.strip() == "stopped"
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_reparenting_stops_the_runtime():
    stop = threading.Event()
    r, w = os.pipe()
    stdin = os.fdopen(r, "rb")
    ppids = iter([4242, 4242, 1])
    try:
        parent_watch.watch_parent(
            stop, stdin=stdin, getppid=lambda: next(ppids, 1), poll_interval=0.01
        )
        assert stop.wait(2)
    finally:
        os.close(w)  # EOF first: closing a reader mid-read would block on its lock
        time.sleep(0.05)
        stdin.close()


def test_watch_needs_an_explicit_opt_in():
    assert parent_watch.parent_watch_enabled({"HERDECK_PARENT_WATCH": "1"})
    # A managed smoke run with </dev/null must NOT exit on stdin EOF.
    assert not parent_watch.parent_watch_enabled({"HERDECK_RUNTIME_MANAGED": "1"})
    assert not parent_watch.parent_watch_enabled({})


class _App:
    host, port, token, source_name = "127.0.0.1", 1, "t", "live"

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _Sink:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_runtime_main_takes_the_sigterm_cleanup_path_on_stdin_eof(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDECK_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("HERDECK_PARENT_WATCH", "1")
    monkeypatch.delenv("HERDECK_RUNTIME_MANAGED", raising=False)
    monkeypatch.delenv("HERDECK_SELFTEST", raising=False)
    app, sink = _App(), _Sink()
    path = tmp_path / "runtime.json"

    def fake_build(**kw):
        path.write_text("{}")
        return app, sink, {"url": "x"}, str(path)

    monkeypatch.setattr(runtime, "build_runtime", fake_build)
    monkeypatch.setattr(runtime, "configure_logging", lambda **kw: None)
    monkeypatch.setattr(runtime.signal, "signal", lambda *a: None)
    r, w = os.pipe()
    reader = os.fdopen(r, "rb")
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": reader})())
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("rc", runtime.main()))
    thread.start()
    time.sleep(0.05)
    assert thread.is_alive()
    os.close(w)  # the shell died
    thread.join(5)
    reader.close()
    assert result == {"rc": 0}
    assert sink.closed and app.closed
    assert not path.exists()  # its own discovery file is removed
