import os


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
