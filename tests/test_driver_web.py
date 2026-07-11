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
    assert "btns.forEach(b=>b.classList.remove('active'))" in page
    assert "if(btns[i]){" in page  # the transient flash is still tile-only


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
    assert "--cell" in block  # tiles resize via the shared cell variable
    # a short viewport may also be narrow (e.g. 320x400) where this rule overrides
    # the portrait one, so it must constrain width too or the deck overflows sideways
    assert "vw" in block


def test_page_uses_readable_desktop_scale():
    from herdeck.driver import web

    page = web._PAGE
    # the desktop scale lives in --cell, derived from the column count with
    # padding + gaps inside the budget (no sideways overflow on any grid)
    assert "2*var(--pad) - (var(--cols) - 1)*var(--gap))/var(--cols)),150px)" in page
    assert "grid-template-columns:repeat(5,var(--cell))" in page
    assert ".cell{width:var(--cell);height:var(--cell);" in page
    assert (
        "#panel{grid-column:4 / 6;width:calc(var(--cell)*2 + var(--gap));"
        "height:var(--cell);"
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
    import io as _io

    from PIL import Image

    d = make_deck()
    d.render_panel(PanelView("dev", ["online"], "grey"))
    assert d._state()["has_panel"] is True
    png = d._panel_png()
    assert png[:4] == b"\x89PNG"
    # The page fills a 2-cells-wide box with the image (width/height 100%), so
    # the PNG must be the two-cell composite — the native 458px would squeeze.
    with Image.open(_io.BytesIO(png)) as im:
        assert im.size == (392, 196)


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


def test_existing_leaky_token_file_is_repaired_to_0600(tmp_path):
    token_file = tmp_path / "web-token"
    token_file.write_text("existing-token")
    token_file.chmod(0o644)  # pre-existing world-readable file
    d = WebDeck(slots=13, serve=False, icon_provider=StubIcons(), token_path=str(token_file))
    assert d.press_token == "existing-token"  # bookmark keeps working
    assert (token_file.stat().st_mode & 0o777) == 0o600  # ...but the leak is fixed


def _get_json(d, path):
    import json as _json
    import urllib.request

    sep = "&" if "?" in path else "?"
    with urllib.request.urlopen(
        f"http://{d.host}:{d.port}{path}{sep}token={d.press_token}", timeout=10
    ) as r:
        return _json.loads(r.read().decode())


def test_state_long_poll_holds_until_a_change(tmp_path):
    """/state?since=<current> must hold until the version advances
    (audit: websim-long-poll)."""
    import threading
    import time as _time

    d = WebDeck(
        slots=4, host="127.0.0.1", port=0, serve=True,
        icon_provider=VaryingIcons(), token_path=str(tmp_path / "web-token"),
    )
    try:
        d.render([TileView(0, "a", "green")])
        v = _get_json(d, "/state")["version"]
        result = {}

        def held():
            t0 = _time.monotonic()
            result["state"] = _get_json(d, f"/state?since={v}")
            result["elapsed"] = _time.monotonic() - t0

        t = threading.Thread(target=held)
        t.start()
        _time.sleep(0.2)
        d.render([TileView(0, "a", "amber")])  # version bumps -> poll releases
        t.join(timeout=5)
        assert result["state"]["version"] > v
        assert 0.1 < result["elapsed"] < 5  # held, then released by the change
    finally:
        d.close()


def test_state_long_poll_times_out_unchanged(tmp_path):
    d = WebDeck(
        slots=4, host="127.0.0.1", port=0, serve=True,
        icon_provider=StubIcons(), token_path=str(tmp_path / "web-token"),
    )
    d.LONG_POLL_TIMEOUT = 0.1
    try:
        d.render([TileView(0, "a", "green")])
        v = _get_json(d, "/state")["version"]
        state = _get_json(d, f"/state?since={v}")
        assert state["version"] == v  # answered with the unchanged state
    finally:
        d.close()


def test_state_carries_grid_cols_and_page_applies_them(tmp_path):
    """The page hardcoded a 5-wide grid while slots follow [deck].grid
    (audit: websim-grid-var)."""
    d = WebDeck(
        slots=6, cols=4, host="127.0.0.1", port=0, serve=True,
        icon_provider=StubIcons(), token_path=str(tmp_path / "web-token"),
    )
    try:
        assert _get_json(d, "/state")["cols"] == 4
        import urllib.request

        with urllib.request.urlopen(
            f"http://{d.host}:{d.port}/?token={d.press_token}", timeout=5
        ) as r:
            page = r.read().decode()
        assert "applyGrid(s.cols)" in page
        assert "setTimeout(()=>b.classList.remove('active'),350)" in page  # transient outline
    finally:
        d.close()


def test_page_speaks_czech_when_configured(tmp_path):
    d = WebDeck(
        4, host="127.0.0.1", port=0,
        icon_provider=StubIcons(), token_path=str(tmp_path / "web-token"),
        language="cs",
    )
    try:
        with urllib.request.urlopen(
            f"http://{d.host}:{d.port}/?token={d.press_token}", timeout=5
        ) as r:
            page = r.read().decode()
        assert "stisk selhal — odpojeno?" in page
        assert "token vypršel" in page
        assert "ŽIVĚ" in page
        assert "JEN ČTENÍ" in page
    finally:
        d.close()


# --- read-only live terminal preview ----------------------------------------


class TerminalSub:
    def __init__(self):
        import queue

        self.queue = queue.Queue(maxsize=120)


def _term_url(
    deck, stream="stream-0001", *, index=0, cols=80, rows=24, tile_version=None
):
    if tile_version is None:
        tile_version = deck._state()["tiles"].get(index)
    if tile_version is None:
        deck.render([TileView(index, "terminal", "blue")])
        tile_version = deck._state()["tiles"][index]
    return (
        f"http://{deck.host}:{deck.port}/term/{index}"
        f"?stream={stream}&v={tile_version}&cols={cols}&rows={rows}"
        f"&token={deck.press_token}"
    )


def test_web_terminal_assets_are_served_with_correct_content_types(tmp_path):
    d = WebDeck(
        slots=4,
        host="127.0.0.1",
        port=0,
        serve=True,
        icon_provider=StubIcons(),
        token_path=str(tmp_path / "web-token"),
    )
    try:
        for name, content_type, marker in (
            ("xterm.js", "text/javascript", b"Terminal"),
            ("addon-fit.js", "text/javascript", b"FitAddon"),
            ("xterm.css", "text/css", b".xterm"),
        ):
            with urllib.request.urlopen(
                f"http://{d.host}:{d.port}/assets/{name}", timeout=5
            ) as response:
                assert response.headers.get_content_type() == content_type
                assert marker in response.read()
    finally:
        d.close()


def test_terminal_sse_streams_provider_items_and_closes_subscription(tmp_path):
    import json

    d = WebDeck(
        slots=4,
        host="127.0.0.1",
        port=0,
        serve=True,
        icon_provider=StubIcons(),
        token_path=str(tmp_path / "web-token"),
    )
    sub = TerminalSub()
    sub.queue.put_nowait({"kind": "meta", "label": "api"})
    sub.queue.put_nowait(
        {"kind": "frame", "seq": 1, "full": True, "cols": 80, "rows": 24, "data": "QQ=="}
    )
    sub.queue.put_nowait({"kind": "closed", "reason": "pane gone"})
    opened = []
    closed = []
    d.on_terminal(
        lambda index, cols, rows, version: opened.append(
            (index, cols, rows, version)
        )
        or sub,
        lambda subscription: closed.append(subscription),
    )
    try:
        with urllib.request.urlopen(_term_url(d), timeout=5) as response:
            assert response.headers.get_content_type() == "text/event-stream"
            events = [
                json.loads(line.removeprefix("data: "))
                for line in response.read().decode().splitlines()
                if line.startswith("data: ")
            ]
        assert opened == [(0, 80, 24, d._state()["tiles"][0])]
        assert [event["kind"] for event in events] == ["meta", "frame", "closed"]
        assert closed == [sub]
        assert d._terminal_streams == {}
    finally:
        d.close()


def test_terminal_stop_endpoint_wakes_idle_stream_and_is_idempotent(tmp_path):
    import threading

    d = WebDeck(
        slots=4,
        host="127.0.0.1",
        port=0,
        serve=True,
        icon_provider=StubIcons(),
        token_path=str(tmp_path / "web-token"),
    )
    sub = TerminalSub()
    opened = threading.Event()
    closed = threading.Event()

    def open_terminal(index, cols, rows, version):
        opened.set()
        return sub

    d.on_terminal(open_terminal, lambda subscription: closed.set())
    result = {}

    def consume():
        with urllib.request.urlopen(_term_url(d, "stream-cancel"), timeout=5) as response:
            result["body"] = response.read()

    thread = threading.Thread(target=consume)
    thread.start()
    try:
        assert opened.wait(2)
        stop = urllib.request.Request(
            f"http://{d.host}:{d.port}/term-stop/stream-cancel",
            method="POST",
            headers={"X-Herdeck-Token": d.press_token},
        )
        with urllib.request.urlopen(stop, timeout=5) as response:
            assert response.status == 204
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert closed.wait(1)
        assert d._terminal_streams == {}

        with urllib.request.urlopen(stop, timeout=5) as response:
            assert response.status == 204
    finally:
        d.close()
        thread.join(timeout=2)


def test_terminal_provider_exception_releases_stream_capacity(tmp_path):
    d = WebDeck(
        slots=4,
        host="127.0.0.1",
        port=0,
        serve=True,
        icon_provider=StubIcons(),
        token_path=str(tmp_path / "web-token"),
    )

    def fail(index, cols, rows, version):
        raise RuntimeError("boom")

    d.on_terminal(fail, lambda subscription: None)
    try:
        with urllib.request.urlopen(_term_url(d, "stream-error"), timeout=5) as response:
            body = response.read().decode()
        assert '"kind": "closed"' in body
        assert d._terminal_streams == {}
    finally:
        d.close()


def test_terminal_endpoint_authenticates_and_clamps_dimensions(tmp_path):
    d = WebDeck(
        slots=4,
        host="127.0.0.1",
        port=0,
        serve=True,
        icon_provider=StubIcons(),
        token_path=str(tmp_path / "web-token"),
    )
    seen = []

    def open_terminal(index, cols, rows, version):
        sub = TerminalSub()
        seen.append((index, cols, rows, version))
        sub.queue.put_nowait({"kind": "closed", "reason": "done"})
        return sub

    d.on_terminal(open_terminal, lambda subscription: None)
    try:
        unauthenticated = _term_url(d, "stream-auth").replace(
            f"&token={d.press_token}", ""
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(unauthenticated, timeout=5)
        assert exc.value.code == 403

        with urllib.request.urlopen(
            _term_url(d, "stream-clamp", cols=1, rows=999), timeout=5
        ) as response:
            response.read()
        assert seen == [(0, 20, 100, d._state()["tiles"][0])]
    finally:
        d.close()


def test_terminal_endpoint_rejects_stale_tile_version_before_provider_start(tmp_path):
    d = WebDeck(
        slots=4,
        host="127.0.0.1",
        port=0,
        serve=True,
        icon_provider=VaryingIcons(),
        token_path=str(tmp_path / "web-token"),
    )
    d.render([TileView(0, "visible", "blue")])
    visible_version = d._state()["tiles"][0]
    d.render([TileView(0, "replacement", "blue")])
    opened = []
    d.on_terminal(
        lambda index, cols, rows, version: opened.append(
            (index, cols, rows, version)
        )
        or TerminalSub(),
        lambda subscription: None,
    )
    stale_url = (
        f"http://{d.host}:{d.port}/term/0?stream=stream-stale-tile"
        f"&v={visible_version}&cols=80&rows=24&token={d.press_token}"
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(stale_url, timeout=5)
        assert exc.value.code == 409
        assert opened == []
        assert d._terminal_streams == {}
    finally:
        d.close()


def test_terminal_stream_limit_uses_real_concurrent_streams_and_releases_slots(tmp_path):
    import json
    import threading

    d = WebDeck(
        slots=4,
        host="127.0.0.1",
        port=0,
        serve=True,
        icon_provider=StubIcons(),
        token_path=str(tmp_path / "web-token"),
    )
    condition = threading.Condition()
    subscriptions = []
    closed = []

    def open_terminal(index, cols, rows, version):
        sub = TerminalSub()
        with condition:
            subscriptions.append(sub)
            condition.notify_all()
        return sub

    d.on_terminal(open_terminal, lambda subscription: closed.append(subscription))
    threads = []

    def consume(stream_id):
        with urllib.request.urlopen(_term_url(d, stream_id), timeout=5) as response:
            response.read()

    try:
        for number in range(d.TERMINAL_STREAM_LIMIT):
            thread = threading.Thread(target=consume, args=(f"stream-cap-{number}",))
            threads.append(thread)
            thread.start()
        with condition:
            assert condition.wait_for(
                lambda: len(subscriptions) == d.TERMINAL_STREAM_LIMIT,
                timeout=2,
            )

        with urllib.request.urlopen(_term_url(d, "stream-cap-busy"), timeout=5) as response:
            events = [
                json.loads(line.removeprefix("data: "))
                for line in response.read().decode().splitlines()
                if line.startswith("data: ")
            ]
        assert events == [{"kind": "closed", "reason": "too many open previews"}]
        assert len(subscriptions) == d.TERMINAL_STREAM_LIMIT

        for number in range(d.TERMINAL_STREAM_LIMIT):
            stop = urllib.request.Request(
                f"http://{d.host}:{d.port}/term-stop/stream-cap-{number}",
                method="POST",
                headers={"X-Herdeck-Token": d.press_token},
            )
            with urllib.request.urlopen(stop, timeout=5) as response:
                assert response.status == 204
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()
        assert len(closed) == d.TERMINAL_STREAM_LIMIT
        assert d._terminal_streams == {}
    finally:
        d.close()
        for thread in threads:
            thread.join(timeout=2)


def test_terminal_stop_arriving_before_get_prevents_late_provider_start(tmp_path):
    d = WebDeck(
        slots=4,
        host="127.0.0.1",
        port=0,
        serve=True,
        icon_provider=StubIcons(),
        token_path=str(tmp_path / "web-token"),
    )
    opened = []
    d.on_terminal(
        lambda index, cols, rows, version: opened.append(
            (index, cols, rows, version)
        )
        or TerminalSub(),
        lambda subscription: None,
    )
    try:
        stop = urllib.request.Request(
            f"http://{d.host}:{d.port}/term-stop/stream-before-get",
            method="POST",
            headers={"X-Herdeck-Token": d.press_token},
        )
        with urllib.request.urlopen(stop, timeout=5) as response:
            assert response.status == 204
        with urllib.request.urlopen(
            _term_url(d, "stream-before-get"), timeout=5
        ) as response:
            response.read()
        assert opened == []
        assert d._terminal_streams == {}
    finally:
        d.close()


def test_page_includes_accessible_generation_safe_terminal_overlay(tmp_path):
    d = WebDeck(
        slots=4,
        host="127.0.0.1",
        port=0,
        serve=True,
        icon_provider=StubIcons(),
        token_path=str(tmp_path / "web-token"),
    )
    try:
        with urllib.request.urlopen(
            f"http://{d.host}:{d.port}/?token={d.press_token}", timeout=5
        ) as response:
            page = response.read().decode()
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["Referrer-Policy"] == "no-referrer"
            assert response.headers["X-Content-Type-Options"] == "nosniff"

        assert 'href="/assets/xterm.css"' in page
        assert 'src="/assets/xterm.js"' in page
        assert 'src="/assets/addon-fit.js"' in page
        assert 'role="dialog"' in page and 'aria-modal="true"' in page
        assert 'aria-live="polite"' in page
        assert "if(preview!==current)return" in page  # stale EventSource generation guard
        assert "Math.max(20,Math.min(240" in page
        assert "Math.max(5,Math.min(100" in page
        assert "'/term-stop/'" in page
        assert "e.button!==0" in page  # secondary pointerdown never arms long-press
        assert "suppressNextClick" in page and "suppressClickUntil" not in page
        assert "cancelLongPress();suppressNextClick=false" in page
        assert "suppressNextClick&&e.detail!==0" in page
        assert "if(openedByLongPress&&!tover.hidden)return" in page
        assert "e.shiftKey&&e.key==='Enter'" in page
        assert "lastPreviewFocus" in page and ".focus()" in page
        assert "if(e.key==='Escape'" in page
        assert "if(document.activeElement===tclose)tterm.focus()" in page
        assert "tlive.textContent=L.termLive" in page
        assert "tlive.textContent=L.termEndedBadge" in page
        assert "longVersion=tv[i]" in page
        assert "'&v='+current.tileVersion" in page
        assert "candidate.onload" in page
        assert "tv[i]=v;delete pendingTv[i]" in page
    finally:
        d.close()


def test_embedded_page_javascript_parses_with_node():
    import re
    import shutil
    import subprocess

    from herdeck.driver import web

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    scripts = re.findall(r"<script>(.*?)</script>", web._PAGE, flags=re.DOTALL)
    assert len(scripts) == 1
    source = scripts[0].replace("__PRESS_TOKEN_JSON__", '"token"').replace(
        "__L_JSON__", "{}"
    )
    result = subprocess.run(
        [node, "--check", "-"],
        input=source,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
