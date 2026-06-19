import pytest

from herdeck.app import make_deck, resolve_mode
from herdeck.driver.fake import FakeRenderer

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


class _Web:
    def __init__(self):
        self.kind = "web"


def _boom():
    raise RuntimeError("no device")


def test_auto_falls_back_to_web_when_d200_unavailable():
    deck = make_deck(None, 13, d200_factory=_boom, web_factory=_Web)
    assert isinstance(deck, _Web)


def test_explicit_d200_failure_propagates():
    with pytest.raises(RuntimeError):
        make_deck("d200", 13, d200_factory=_boom, web_factory=_Web)


def test_fake_kind_returns_fake_renderer():
    deck = make_deck("fake", 13, d200_factory=_boom, web_factory=_Web)
    assert isinstance(deck, FakeRenderer)


def test_unknown_explicit_deck_kind_raises():
    with pytest.raises(ValueError, match="unsupported deck kind"):
        make_deck("dw00", 13, d200_factory=_boom, web_factory=_Web)
