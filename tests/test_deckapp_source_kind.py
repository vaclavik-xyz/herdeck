from herdeck.deckapp.server import select_source_kind

REMOTE = ("CONFIG", "SERVER")  # opaque sentinels; the function just passes them through


def k(**kw):
    base = dict(mock_env=False, remote=None, choice=None, socket_path="/s.sock", socket_exists=False)
    base.update(kw)
    return select_source_kind(**base)


def test_mock_env_wins():
    assert k(mock_env=True, remote=REMOTE, choice="local", socket_exists=True) == ("mock", "mock_env")


def test_remote_config():
    assert k(remote=REMOTE) == ("remote", "CONFIG", "SERVER")


def test_local_choice_overrides_remote_config():
    # an explicit local choice wins over a remote config on disk (sticks across restart)
    assert k(remote=REMOTE, choice="local", socket_exists=True) == ("local", "/s.sock")


def test_demo_choice_overrides_remote_config():
    assert k(remote=REMOTE, choice="demo") == ("mock", "demo")


def test_local_when_socket_present():
    assert k(choice="local", socket_exists=True) == ("local", "/s.sock")


def test_local_choice_but_socket_missing():
    assert k(choice="local", socket_exists=False) == ("mock", "local_unavailable")


def test_demo():
    assert k(choice="demo") == ("mock", "demo")


def test_first_run():
    assert k() == ("mock", "first_run")
