import io
import os
import urllib.error
import urllib.request

import pytest
from PIL import Image

from herdeck.driver.base import PanelView, TileView
from herdeck.driver.web import WebDeck


@pytest.fixture(autouse=True)
def _isolated_token_state(tmp_path, monkeypatch):
    """Serving decks persist their press token under XDG_STATE_HOME — keep
    tests out of the user's real state dir."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


class StubIcons:
    def render_tile_bytes(self, tile):
        b = io.BytesIO()
        Image.new("RGB", (10, 10), (1, 2, 3)).save(b, "PNG")
        return b.getvalue()


def make_deck():
    return WebDeck(slots=13, serve=False, icon_provider=StubIcons())


def test_web_deck_icons_dir_configures_override_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))

    deck = WebDeck(slots=13, serve=False, icons_dir="~/herdeck-icons")

    assert deck._icons._overrides_dir == os.path.join(str(tmp_path), "herdeck-icons")


def test_page_has_keyboard_and_highlight_support():
    # the page is the module-level _PAGE served at "/"
    from herdeck.driver import web

    page = web._PAGE
    assert "keydown" in page  # keyboard shortcuts wired
    assert "@media" in page  # responsive layout present
    assert "press" in page  # still posts presses


def test_page_guards_key_repeat_and_panel_clears_highlight():
    from herdeck.driver import web

    page = web._PAGE
    # auto-repeat must not spam presses while a number key is held down
    assert "e.repeat" in page
    # clearing the highlight is unconditional; only the add is guarded by btns[i],
    # so a panel press (no button) still clears any stale tile outline
    assert "if(btns[i]) btns[i].classList.add('active')" in page


def test_page_uses_state_slot_count_for_cells_and_panel_index():
    from herdeck.driver import web

    page = web._PAGE
    assert "ensureCells(s.slots)" in page
    assert "for(let i=0;i<slotCount;i++)" in page
    assert "press(slotCount)" in page
    assert "for(let i=0;i<13" not in page
    assert "press(13)" not in page


def test_page_only_highlights_after_successful_press():
    from herdeck.driver import web

    page = web._PAGE
    # a 403 press no longer blind-reloads (that landed on the plaintext 403);
    # it surfaces the token problem in-page instead
    assert "if(r.status===403){ setStale(" in page
    assert "if(!r.ok) return" in page
    assert page.index("await fetch") < page.index("btns.forEach")


def test_page_landscape_rule_sizes_deck_for_short_height():
    from herdeck.driver import web

    page = web._PAGE
    # phone landscape limits HEIGHT, not width: a max-height media rule must exist
    marker = "@media (max-height:"
    assert marker in page
    # extract the media block by brace-matching, then assert it sizes by viewport
    # height (vh) so the 3-row deck fits a short landscape viewport
    start = page.index(marker)
    open_brace = page.index("{", start)
    depth = 0
    end = open_brace
    for end in range(open_brace, len(page)):
        if page[end] == "{":
            depth += 1
        elif page[end] == "}":
            depth -= 1
            if depth == 0:
                break
    block = page[start : end + 1]
    assert "vh" in block  # cells sized by viewport height, not just width
    assert ".cell" in block  # the tiles themselves are resized
    # a short viewport may also be narrow (e.g. 320x400) where this rule overrides
    # the portrait one, so it must constrain width too or the deck overflows sideways
    assert "vw" in block


def test_page_uses_readable_desktop_scale():
    from herdeck.driver import web

    page = web._PAGE
    assert "grid-template-columns:repeat(5,min(17vw,150px))" in page
    assert ".cell{width:min(17vw,150px);height:min(17vw,150px);" in page
    assert (
        "#panel{grid-column:4 / 6;width:calc(min(17vw,150px)*2 + 10px);"
        "height:min(17vw,150px);"
    ) in page


def test_render_updates_state_and_serves_png():
    d = make_deck()
    d.render(
        [
            TileView(
                0,
                "",
                "amber",
                agent_type="claude",
                repo="api",
                branch="x",
                status_text="BLOCKED",
                time_text="1m",
            )
        ]
    )
    st = d._state()
    assert st["version"] >= 1 and 0 in st["tiles"]
    assert d._tile_png(0)[:4] == b"\x89PNG"
    assert d._tile_png(5) is None


def test_render_panel_serves_png():
    d = make_deck()
    d.render_panel(PanelView("dev", ["online"], "grey"))
    assert d._state()["has_panel"] is True
    assert d._panel_png()[:4] == b"\x89PNG"


def test_press_invokes_callback():
    d = make_deck()
    seen = []
    d.on_press(seen.append)
    d.press(7)
    assert seen == [7]


def test_version_bumps_on_each_render():
    d = make_deck()
    v0 = d._state()["version"]
    d.render([TileView(0, "Stop", "red")])
    assert d._state()["version"] > v0


def test_press_ignores_out_of_range_indices():
    d = make_deck()
    seen = []
    d.on_press(seen.append)
    d.press(-1)  # crafted negative index
    d.press(99)  # beyond panel cells
    d.press(13)  # panel cell — valid
    assert seen == [13]


def test_http_press_requires_session_token():
    d = WebDeck(slots=4, host="127.0.0.1", port=0, serve=True, icon_provider=StubIcons())
    seen = []
    d.on_press(seen.append)
    try:
        url = f"http://{d.host}:{d.port}/press/0"
        req = urllib.request.Request(url, method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=2)
        assert exc.value.code == 403
        assert seen == []

        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://{d.host}:{d.port}/", timeout=2)
        assert exc.value.code == 403

        with urllib.request.urlopen(
            f"http://{d.host}:{d.port}/?token={d._press_token}", timeout=2
        ) as resp:
            assert d._press_token in resp.read().decode()

        for path in ("/state", "/panel", "/tile/0"):
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(f"http://{d.host}:{d.port}{path}", timeout=2)
            assert exc.value.code == 403

        with urllib.request.urlopen(
            f"http://{d.host}:{d.port}/state?token={d._press_token}", timeout=2
        ) as resp:
            assert resp.status == 200

        req = urllib.request.Request(url, method="POST", headers={"X-Herdeck-Token": "é"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=2)
        assert exc.value.code == 403
        assert seen == []

        req = urllib.request.Request(
            url, method="POST", headers={"X-Herdeck-Token": d._press_token}
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            assert resp.status == 204
        assert seen == [0]
    finally:
        d.close()


def test_error_responses_are_browser_friendly_not_downloads():
    # A request without a valid token must return viewable text, never
    # application/octet-stream — Safari offers to download octet-stream bodies.
    d = WebDeck(slots=4, host="127.0.0.1", port=0, serve=True, icon_provider=StubIcons())
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://{d.host}:{d.port}/", timeout=2)
        err = exc.value
        assert err.code == 403
        # "/" now serves a readable HTML explanation; the point stands:
        # viewable text, never octet-stream.
        assert err.headers.get("Content-Type", "").startswith("text/")
        assert "token" in err.read().decode().lower()
    finally:
        d.close()


def test_close_releases_server_socket():
    d = WebDeck(slots=4, host="127.0.0.1", port=0, serve=True, icon_provider=StubIcons())
    port = d.port
    d.close()

    replacement = WebDeck(
        slots=4, host="127.0.0.1", port=port, serve=True, icon_provider=StubIcons()
    )
    replacement.close()


class VaryingIcons:
    """Icon stub whose bytes depend on the tile's visible content, so identical
    tiles render identical bytes and changed tiles render different bytes."""

    def render_tile_bytes(self, tile):
        sig = f"{tile.index}|{tile.label}|{tile.color}|{tile.spinner}|{tile.status_text}"
        return sig.encode()


def make_varying_deck():
    return WebDeck(slots=13, serve=False, icon_provider=VaryingIcons())


def test_state_reports_per_tile_versions():
    d = make_varying_deck()
    d.render([TileView(0, "a", "blue"), TileView(1, "b", "green")])
    tiles = d._state()["tiles"]
    assert isinstance(tiles, dict)
    assert tiles[0] >= 1 and tiles[1] >= 1


def test_unchanged_tile_keeps_version_changed_tile_bumps():
    d = make_varying_deck()
    d.render([TileView(0, "a", "blue"), TileView(1, "b", "green")])
    v0 = d._state()["tiles"][0]
    v1 = d._state()["tiles"][1]
    # re-render: tile 0 identical, tile 1 changed (spinner advanced)
    d.render([TileView(0, "a", "blue"), TileView(1, "b", "green", spinner=3)])
    assert d._state()["tiles"][0] == v0  # unchanged -> same version
    assert d._state()["tiles"][1] > v1  # changed -> bumped


def test_identical_full_render_does_not_bump_global_version():
    d = make_varying_deck()
    d.render([TileView(0, "a", "blue")])
    v = d._state()["version"]
    d.render([TileView(0, "a", "blue")])  # nothing changed
    assert d._state()["version"] == v


def test_panel_version_bumps_only_on_change():
    d = make_varying_deck()
    d.render_panel(PanelView("dev", ["x"], "grey"))
    pv = d._state()["panel"]
    assert pv >= 1
    d.render_panel(PanelView("dev", ["x"], "grey"))  # identical content
    assert d._state()["panel"] == pv  # no bump
    d.render_panel(PanelView("dev", ["y"], "grey"))  # changed content
    assert d._state()["panel"] > pv  # bumped


def test_render_working_updates_only_given_tiles():
    d = make_varying_deck()
    d.render([TileView(0, "a", "blue"), TileView(1, "b", "green")])
    v0 = d._state()["tiles"][0]
    v1 = d._state()["tiles"][1]
    d.render_working([TileView(1, "b", "green", spinner=5)])  # partial: only tile 1
    st = d._state()
    assert st["tiles"][0] == v0  # untouched tile keeps its version
    assert st["tiles"][1] > v1  # working tile bumped
    assert d._tile_png(1) == VaryingIcons().render_tile_bytes(
        TileView(1, "b", "green", spinner=5)
    )  # new bytes served


def test_render_working_leaves_panel_untouched():
    d = make_varying_deck()
    d.render_panel(PanelView("dev", ["x"], "grey"))
    pv = d._state()["panel"]
    d.render_working([TileView(0, "a", "blue", spinner=2)])
    assert d._state()["panel"] == pv


def test_render_working_skips_unchanged_tile():
    d = make_varying_deck()
    d.render([TileView(0, "a", "blue")])
    v0 = d._state()["tiles"][0]
    d.render_working([TileView(0, "a", "blue")])  # identical content -> no bump
    assert d._state()["tiles"][0] == v0


def test_render_removing_a_tile_bumps_version_so_client_can_clear_it():
    d = make_varying_deck()
    d.render([TileView(0, "a", "blue"), TileView(1, "b", "green")])
    v = d._state()["version"]
    d.render([TileView(0, "a", "blue")])  # tile 1 omitted (tile 0 unchanged)
    st = d._state()
    assert st["version"] > v  # removal trips the client's gate
    assert 1 not in st["tiles"]  # its version is dropped
    assert d._tile_png(1) is None  # its bytes are gone


# --- press-token persistence (audit: websim-token-persist) --------------------


def test_token_persists_across_restarts(tmp_path):
    token_file = tmp_path / "web-token"
    a = WebDeck(slots=13, serve=False, icon_provider=StubIcons(), token_path=str(token_file))
    b = WebDeck(slots=13, serve=False, icon_provider=StubIcons(), token_path=str(token_file))
    assert a.press_token == b.press_token  # the phone's bookmarked URL survives
    assert (token_file.stat().st_mode & 0o777) == 0o600


def test_non_serving_deck_keeps_ephemeral_token(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))  # belt & braces isolation
    a = WebDeck(slots=13, serve=False, icon_provider=StubIcons())
    b = WebDeck(slots=13, serve=False, icon_provider=StubIcons())
    assert a.press_token != b.press_token
    assert not (tmp_path / "herdeck" / "web-token").exists()


def test_root_403_serves_html_explanation(tmp_path):
    import urllib.error
    import urllib.request

    d = WebDeck(
        slots=4, host="127.0.0.1", port=0, serve=True,
        icon_provider=StubIcons(), token_path=str(tmp_path / "web-token"),
    )
    try:
        try:
            urllib.request.urlopen(f"http://{d.host}:{d.port}/?token=wrong", timeout=5)
            raise AssertionError("expected HTTP 403")
        except urllib.error.HTTPError as e:
            assert e.code == 403
            assert e.headers.get_content_type() == "text/html"
            body = e.read().decode()
            assert "token" in body.lower()
    finally:
        d.close()


def test_page_carries_stale_indicator_and_touch_feedback(tmp_path):
    """Smoke-level guards for the embedded page: stale-state indication,
    explicit 403 handling, wake-refresh and touch affordances
    (audit: websim-stale-indicator + websim-touch-feedback)."""
    import urllib.request

    d = WebDeck(
        slots=4, host="127.0.0.1", port=0, serve=True,
        icon_provider=StubIcons(), token_path=str(tmp_path / "web-token"),
    )
    try:
        with urllib.request.urlopen(
            f"http://{d.host}:{d.port}/?token={d.press_token}", timeout=5
        ) as r:
            page = r.read().decode()
        assert "setStale" in page and "disconnected — last update" in page
        assert "token expired" in page  # explicit 403 handling, no silent freeze
        assert "visibilitychange" in page  # immediate poll on phone wake
        assert "touch-action:manipulation" in page
        assert "-webkit-tap-highlight-color" in page
        assert ".cell:active" in page  # instant local press feedback
        assert "pollNow()" in page  # press triggers an immediate state poll
    finally:
        d.close()
