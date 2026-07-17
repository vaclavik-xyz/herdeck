import time

from herdeck.deckapp.watcher import ConfigWatcher


def test_close_before_start_is_safe(tmp_path):
    w = ConfigWatcher([tmp_path / "x.toml"], lambda: None)
    w.close()  # must not raise RuntimeError


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
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("nope")

    w = ConfigWatcher([f], boom, interval=0.02)
    w.start()
    try:
        f.write_text("a = 2\n")
        deadline = time.monotonic() + 1.0
        while not calls and time.monotonic() < deadline:
            time.sleep(0.02)
        assert calls  # the raising callback WAS invoked
        # give the loop a couple more cycles; it must not have died
        time.sleep(0.1)
        assert w._thread.is_alive()  # survived the raising callback
    finally:
        w.close()


def test_dynamic_path_provider_notices_selected_socket_appearing(tmp_path):
    config = tmp_path / "local.toml"
    config.write_text('[local]\nherdr_sessions = ["review"]\n')
    socket = tmp_path / "sessions" / "review" / "herdr.sock"
    w = ConfigWatcher(
        [config],
        lambda: None,
        paths_provider=lambda: [str(socket)],
    )

    assert w.dirty() is False
    socket.parent.mkdir(parents=True)
    socket.touch()
    assert w.dirty() is True
