import subprocess

from herdeck.terminal_app import activate_terminal_app


def _inline(fn):
    fn()


def test_activates_named_app_on_macos():
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw["timeout"]))
        return subprocess.CompletedProcess(argv, 0)

    assert activate_terminal_app("Ghostty", platform="darwin", run=run, spawn=_inline)
    assert calls == [(["open", "-a", "Ghostty"], 5.0)]


def test_empty_name_is_off():
    run_calls = []
    assert not activate_terminal_app("", platform="darwin", run=run_calls.append, spawn=_inline)
    assert not activate_terminal_app("  ", platform="darwin", run=run_calls.append, spawn=_inline)
    assert not activate_terminal_app(None, platform="darwin", run=run_calls.append, spawn=_inline)
    assert run_calls == []


def test_non_macos_is_a_noop():
    run_calls = []
    assert not activate_terminal_app("Ghostty", platform="linux", run=run_calls.append, spawn=_inline)
    assert run_calls == []


def test_failures_never_raise():
    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 5)

    assert activate_terminal_app("Ghostty", platform="darwin", run=boom, spawn=_inline)

    def missing(argv, **kw):
        return subprocess.CompletedProcess(argv, 1)

    assert activate_terminal_app("NoSuchApp", platform="darwin", run=missing, spawn=_inline)


def test_spawn_failure_is_swallowed():
    def bad_spawn(fn):
        raise RuntimeError("no threads")

    assert not activate_terminal_app("Ghostty", platform="darwin", run=None, spawn=bad_spawn)


def test_runs_off_the_calling_thread_by_default():
    import threading

    seen = []
    done = threading.Event()

    def run(argv, **kw):
        seen.append(threading.current_thread() is threading.main_thread())
        done.set()
        return subprocess.CompletedProcess(argv, 0)

    assert activate_terminal_app("Ghostty", platform="darwin", run=run)
    assert done.wait(2)
    assert seen == [False]
