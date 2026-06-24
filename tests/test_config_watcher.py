import time

from herdeck.deckapp.watcher import ConfigWatcher


def test_watcher_fires_on_change_and_is_quiet_otherwise(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text("a = 1\n")
    calls = []
    w = ConfigWatcher([f], lambda: calls.append(1), interval=0.02)
    w.start()
    try:
        time.sleep(0.1)
        assert calls == []  # no change -> no fire
        f.write_text("a = 2\n")
        deadline = time.monotonic() + 1.0
        while not calls and time.monotonic() < deadline:
            time.sleep(0.02)
        assert calls == [1]
    finally:
        w.close()


def test_watcher_swallows_callback_errors(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text("a = 1\n")

    def boom():
        raise RuntimeError("nope")

    w = ConfigWatcher([f], boom, interval=0.02)
    w.start()
    try:
        f.write_text("a = 2\n")
        time.sleep(0.2)  # must not crash the daemon thread
        assert w._thread.is_alive()
    finally:
        w.close()
