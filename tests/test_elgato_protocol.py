import pytest

from herdeck.elgato.protocol import PROTOCOL_VERSION, ProtocolError, decode, encode


def test_protocol_version_is_one():
    assert PROTOCOL_VERSION == 1


def test_encode_is_single_json_line():
    raw = encode({"type": "ready"})
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1
    assert decode(raw) == {"type": "ready"}


def test_decode_accepts_str_and_bytes_without_newline():
    assert decode('{"type":"hello"}') == {"type": "hello"}
    assert decode(b'{"type":"hello"}') == {"type": "hello"}


def test_decode_rejects_garbage():
    with pytest.raises(ProtocolError):
        decode(b"not json")


def test_decode_rejects_invalid_utf8():
    with pytest.raises(ProtocolError):
        decode(b"\xff\xfe")
