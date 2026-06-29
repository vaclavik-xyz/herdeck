import os

from herdeck.bootstrap import resolve_socket_path


def test_env_override_wins():
    assert resolve_socket_path(None, getenv={"HERDR_SOCKET": "/tmp/x.sock"}.get) == "/tmp/x.sock"


def test_default_when_unset():
    expected = os.path.expanduser("~/.config/herdr/herdr.sock")
    assert resolve_socket_path(None, getenv={}.get) == expected


def test_config_hardware_override():
    class _HW:
        herdr_socket = "~/custom/herdr.sock"

    class _Cfg:
        hardware = _HW()

    assert resolve_socket_path(_Cfg(), getenv={}.get) == os.path.expanduser("~/custom/herdr.sock")
