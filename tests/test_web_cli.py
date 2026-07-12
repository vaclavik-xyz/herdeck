import os

import pytest


def test_web_url_command_prints_explicit_capability_url(tmp_path, capsys):
    from herdeck.web import main

    token_file = tmp_path / "web-token"
    main(
        [
            "url",
            "--host",
            "100.86.178.12",
            "--port",
            "8800",
            "--token-file",
            str(token_file),
        ]
    )

    output = capsys.readouterr().out.strip()
    token = token_file.read_text()
    assert output == f"http://100.86.178.12:8800/?token={token}"
    assert token_file.stat().st_mode & 0o777 == 0o600


def test_web_run_command_sets_explicit_runtime_and_delegates(monkeypatch):
    from herdeck import web

    seen = []
    monkeypatch.setattr(web, "_app_main", lambda: seen.append("ran"))
    monkeypatch.setenv("HERDECK_DECK", "old")
    monkeypatch.setenv("HERDECK_WEB_BIND", "127.0.0.2")
    monkeypatch.setenv("HERDECK_WEB_PORT", "8801")

    web.main(["run", "--host", "127.0.0.1", "--port", "9911"])

    assert seen == ["ran"]
    assert os.environ["HERDECK_DECK"] == "web"
    assert os.environ["HERDECK_WEB_BIND"] == "127.0.0.1"
    assert os.environ["HERDECK_WEB_PORT"] == "9911"


def test_web_url_command_uses_public_reverse_proxy_url(tmp_path, capsys):
    from herdeck.web import main

    token_file = tmp_path / "web-token"
    main(
        [
            "url",
            "--host",
            "127.0.0.1",
            "--port",
            "8800",
            "--base-path",
            "/cockpit/herdeck",
            "--public-origin",
            "https://cockpit.example",
            "--token-file",
            str(token_file),
        ]
    )

    token = token_file.read_text()
    assert capsys.readouterr().out.strip() == (
        f"https://cockpit.example/cockpit/herdeck/?token={token}"
    )


def test_web_url_command_fails_if_token_cannot_be_persisted(tmp_path, capsys):
    from herdeck.web import main

    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("occupied")

    with pytest.raises(SystemExit, match="persist web token"):
        main(["url", "--token-file", str(parent_file / "web-token")])

    assert capsys.readouterr().out == ""
