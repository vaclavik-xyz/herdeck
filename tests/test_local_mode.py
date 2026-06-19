from herdeck.app import resolve_mode

SOCK = "/Users/x/.config/herdr/herdr.sock"


def test_mock_wins():
    assert resolve_mode(mock=True, config_path="/c", config_has_servers=True,
                        socket_path=SOCK, socket_exists=True) == ("mock",)


def test_config_with_servers_is_remote():
    assert resolve_mode(mock=False, config_path="/c", config_has_servers=True,
                        socket_path=SOCK, socket_exists=True) == ("remote", "/c")


def test_socket_without_servers_is_local():
    assert resolve_mode(mock=False, config_path=None, config_has_servers=False,
                        socket_path=SOCK, socket_exists=True) == ("local", SOCK)


def test_serverless_config_plus_socket_is_local():
    assert resolve_mode(mock=False, config_path="/c", config_has_servers=False,
                        socket_path=SOCK, socket_exists=True) == ("local", SOCK)


def test_no_socket_no_servers_is_error():
    mode = resolve_mode(mock=False, config_path=None, config_has_servers=False,
                        socket_path=SOCK, socket_exists=False)
    assert mode[0] == "error" and SOCK in mode[1]
