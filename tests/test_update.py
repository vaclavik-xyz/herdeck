from __future__ import annotations

import json
import urllib.request

import pytest

from herdeck import __version__, app
from herdeck.update import (
    UpdateCheckError,
    Version,
    _HTTPSRedirectHandler,
    check_for_update,
    main,
)

# One minor above whatever this checkout is. Hard-coding the "newer" release
# works right up until the project reaches that version, at which point the
# update-available tests silently start asserting the opposite of their names —
# which is exactly what happened on the 0.2.0 bump.
NEWER_TAG = "v{}.{}.0".format(
    int(__version__.split(".")[0]),
    int(__version__.split(".")[1].split("-")[0].split("+")[0]) + 1,
)


def _release(tag: str = NEWER_TAG) -> dict:
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/vaclavik-xyz/herdeck/releases/tag/{tag}",
        "published_at": "2026-07-22T10:00:00Z",
    }


def test_version_comparison_follows_semver_prerelease_order():
    assert Version.parse("0.1.0-alpha.1") < Version.parse("0.1.0-alpha.2")
    assert Version.parse("0.1.0-alpha.2") < Version.parse("0.1.0")
    assert Version.parse("0.1.0") < Version.parse("0.2.0")


@pytest.mark.parametrize("value", ["1.0.0-01", "1.0.0-alpha..1", "1.0.0+build..1"])
def test_version_rejects_invalid_semver_identifiers(value):
    with pytest.raises(UpdateCheckError, match="invalid release version"):
        Version.parse(value)


def test_redirect_handler_rejects_https_downgrade():
    handler = _HTTPSRedirectHandler()
    request = urllib.request.Request("https://releases.example/latest.json")

    with pytest.raises(UpdateCheckError, match="must use HTTPS"):
        handler.redirect_request(request, None, 302, "Found", {}, "http://evil.example/update")


def test_check_for_update_reports_newer_release():
    status = check_for_update(
        current_version="0.1.0",
        fetch_json=lambda endpoint: _release(),
    )

    assert status.update_available is True
    assert status.latest_version == NEWER_TAG.lstrip("v")
    assert status.current_version == "0.1.0"


def test_check_for_update_rejects_incomplete_release_response():
    with pytest.raises(UpdateCheckError, match="missing tag_name or html_url"):
        check_for_update(fetch_json=lambda endpoint: {"tag_name": "v0.2.0"})


def test_update_check_json_output(monkeypatch, capsys):
    monkeypatch.setattr("herdeck.update._fetch_json", lambda endpoint: _release())

    assert main(["--check", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["update_available"] is True
    assert payload["latest_version"] == NEWER_TAG.lstrip("v")


def test_update_requires_explicit_check():
    with pytest.raises(SystemExit) as exc:
        main([])

    assert exc.value.code == 2


def test_app_dispatches_update_without_starting_runtime(monkeypatch):
    seen = []
    monkeypatch.setattr("herdeck.update.main", lambda argv: seen.append(argv) or 0)

    with pytest.raises(SystemExit) as exc:
        app.main(["update", "--check"])

    assert exc.value.code == 0
    assert seen == [["--check"]]


def test_app_prints_version(capsys):
    app.main(["--version"])

    assert capsys.readouterr().out == f"herdeck {__version__}\n"
