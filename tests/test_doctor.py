from herdeck.doctor import Check, check_config, check_socket


def test_check_socket_missing():
    c = check_socket("/nope.sock", exists=lambda p: False, probe=None)
    assert isinstance(c, Check) and c.ok is False and "not found" in c.detail.lower()


def test_check_socket_ok():
    c = check_socket("/s.sock", exists=lambda p: True,
                     probe=lambda path: {"result": {"panes": [1, 2]}})
    assert c.ok is True and "2" in c.detail


def test_check_socket_no_response():
    c = check_socket("/s.sock", exists=lambda p: True,
                     probe=lambda path: (_ for _ in ()).throw(TimeoutError()))
    assert c.ok is False and "respond" in c.detail.lower()


def test_check_socket_malformed():
    c = check_socket("/s.sock", exists=lambda p: True, probe=lambda path: {"weird": 1})
    assert c.ok is False


def test_check_socket_malformed_panes_type():
    c = check_socket("/s.sock", exists=lambda p: True,
                     probe=lambda path: {"result": {"panes": "not-a-list"}})
    assert c.ok is False


def test_check_socket_malformed_response_type():
    c = check_socket("/s.sock", exists=lambda p: True, probe=lambda path: [])
    assert c.ok is False


def test_check_socket_malformed_result_type():
    c = check_socket("/s.sock", exists=lambda p: True,
                     probe=lambda path: {"result": []})
    assert c.ok is False


def test_check_config_none_is_local_mode():
    c = check_config(config_path=None, has_servers=False, socket_exists=True,
                     getenv=lambda k: None)
    assert c.ok is True and "local" in c.detail.lower()


def test_check_config_remote_missing_token_redacts():
    c = check_config(config_path="/c", has_servers=True, socket_exists=False,
                     token_envs=["HERDECK_TOKEN"], getenv=lambda k: None)
    assert c.ok is False
    assert "HERDECK_TOKEN" in c.detail and "missing" in c.detail.lower()


def test_check_config_remote_token_present_not_leaked():
    c = check_config(config_path="/c", has_servers=True, socket_exists=False,
                     token_envs=["HERDECK_TOKEN"], getenv=lambda k: "supersecret")
    assert c.ok is True and "supersecret" not in c.detail
