"""GET /themes: the deck looks the editor offers (token-gated)."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest
from PIL import Image

from herdeck.deckapp import DeckApp, MockSource
from herdeck.themes import registry as reg_mod
from herdeck.themes.registry import BUILTIN_MODULES, ThemeRegistry


class _StubIcons:
    def render_tile_bytes(self, tile):
        buf = io.BytesIO()
        Image.new("RGB", (4, 4)).save(buf, "PNG")
        return buf.getvalue()


@pytest.fixture
def app(tmp_path):
    (tmp_path / "mine.py").write_text(
        "from herdeck.themes.api import Theme\n\n\n"
        "class T(Theme):\n    id = 'mine'\n    name = {'en': 'Mine', 'cs': 'Můj'}\n\n\n"
        "THEME = T()\n",
        encoding="utf-8",
    )
    (tmp_path / "broken.py").write_text("def oops(:\n", encoding="utf-8")
    reg_mod.set_default_registry(ThemeRegistry(str(tmp_path)))
    deck = DeckApp(MockSource(), host="127.0.0.1", port=0, serve=True, icon_provider=_StubIcons())
    yield deck
    deck.close()
    reg_mod.set_default_registry(None)


def _get(app, path):
    return urllib.request.urlopen(f"http://{app.host}:{app.port}{path}", timeout=2)


def test_themes_requires_the_token(app):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(app, "/themes")
    assert exc.value.code == 403


def test_themes_lists_builtins_and_user_themes(app):
    with _get(app, f"/themes?token={app.token}") as r:
        assert r.headers.get("Content-Type") == "application/json"
        themes = json.loads(r.read())
    by_id = {t["id"]: t for t in themes}
    builtin_ids = list(BUILTIN_MODULES)
    assert [t["id"] for t in themes[: len(builtin_ids)]] == builtin_ids
    assert all(t["builtin"] and t["error"] is None for t in themes[: len(builtin_ids)])
    assert by_id["classic"]["name"] == {"en": "Classic", "cs": "Klasický"}
    assert by_id["mine"] == {
        "id": "mine", "name": {"en": "Mine", "cs": "Můj"}, "builtin": False, "error": None
    }
    assert by_id["broken"]["error"].startswith("SyntaxError")
