"""The desktop mini dock's data: GET /agent/list and GET /project-icon/<hash>."""

import io

from PIL import Image

from herdeck.deckapp.dock_data import (
    agent_mark_png,
    agent_rows,
    list_signature,
    monogram,
    row_title,
)
from herdeck.model import AgentKey, AgentState, Status
from herdeck.project_icon_discovery import icon_hash
from herdeck.project_icons import ProjectIconStore


def _png(color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (32, 32), color + (255,)).save(buf, "PNG")
    return buf.getvalue()


def _agent(pane, status=Status.IDLE, **kw):
    return AgentState(AgentKey("prod", pane), "claude", f"label-{pane}", status, **kw)


# --- rows ---------------------------------------------------------------------


def test_title_falls_back_to_workspace_repo_then_label():
    assert row_title(_agent("a", title="T", workspace="W", repo="r")) == "T"
    assert row_title(_agent("b", workspace="W", repo="r")) == "W"
    assert row_title(_agent("c", repo="r")) == "r"
    assert row_title(_agent("d")) == "label-d"


def test_monogram_is_the_first_alphanumeric_uppercased():
    assert monogram("macdoktor-crm") == "M"
    assert monogram("-_9lives") == "9"
    assert monogram("--") == "?"


def test_rows_carry_every_field_and_skip_inactive_agents():
    agents = [
        _agent(
            "p0",
            Status.BLOCKED,
            title="Česká bankovní API",
            repo="macdoktor-crm",
            workspace="macDoktor CRM",
            branch="feat/bank",
            waiting_on="",
            subagents_running=2,
        ),
        _agent("p1", repo="shop", lifecycle="closed"),
    ]
    rows = agent_rows(
        agents,
        since={AgentKey("prod", "p0"): 260.7},
        connections={"prod": True},
        overrides={},
        store=ProjectIconStore(),
    )
    assert rows == [
        {
            "server_id": "prod",
            "pane_id": "p0",
            "agent_type": "claude",
            "title": "Česká bankovní API",
            "repo": "macdoktor-crm",
            "workspace": "macDoktor CRM",
            "branch": "feat/bank",
            "tab": "",
            "status": "blocked",
            "since_s": 260,
            "waiting_on": "",
            "subagents": 2,
            "offline": False,
            "icon": None,
            "monogram": "M",
        }
    ]


def test_rows_default_since_to_zero_and_mark_a_down_server_offline():
    rows = agent_rows(
        [_agent("a", Status.WORKING, repo="r")],
        since={},
        connections={"prod": False},
        overrides={},
        store=ProjectIconStore(),
    )
    assert (rows[0]["since_s"], rows[0]["offline"], rows[0]["status"]) == (0, True, "working")


def test_rows_resolve_the_icon_like_tiles_do(tmp_path):
    store = ProjectIconStore()
    png = _png()
    h = icon_hash(png)
    store.put(h, "image/png", png)
    override = tmp_path / "logo.png"
    override.write_bytes(_png((10, 200, 10)))
    rows = agent_rows(
        [
            _agent("a", repo="shop", project_icon=h),
            _agent("b", repo="shop", project_icon="ffffffffffffffff"),
            _agent("c", repo="web", project_icon=h),
        ],
        since={},
        connections={},
        overrides={"web": str(override)},
        store=store,
    )
    assert rows[0]["icon"] == h
    assert rows[1]["icon"] is None  # hash without bytes -> monogram
    assert rows[2]["icon"] == icon_hash(override.read_bytes())  # override wins


# --- signature -----------------------------------------------------------------


def test_signature_ignores_order_and_changes_with_any_row_field():
    store = ProjectIconStore()
    a, b = _agent("a", repo="r"), _agent("b", Status.WORKING, repo="r")
    base = list_signature([a, b], {"prod": True}, "en", store)
    assert list_signature([b, a], {"prod": True}, "en", store) == base
    assert list_signature([a, _agent("b", Status.BLOCKED, repo="r")], {"prod": True}, "en", store) != base
    assert list_signature([a, _agent("b", Status.WORKING, repo="r", title="x")], {"prod": True}, "en", store) != base
    assert list_signature([a, b], {"prod": False}, "en", store) != base
    assert list_signature([a, b], {"prod": True}, "cs", store) != base


def test_signature_changes_when_icon_bytes_arrive():
    store = ProjectIconStore()
    png = _png()
    h = icon_hash(png)
    agents = [_agent("a", repo="shop", project_icon=h)]
    before = list_signature(agents, {}, "en", store)
    store.put(h, "image/png", png)
    assert list_signature(agents, {}, "en", store) != before


def test_signature_changes_with_the_icon_fallback_and_project_icon_overrides():
    store = ProjectIconStore()
    agents = [_agent("a", repo="r")]
    base = list_signature(agents, {}, "en", store)
    # The default spelling is the same signature as passing nothing.
    assert list_signature(agents, {}, "en", store, icon_fallback="monogram") == base
    assert list_signature(agents, {}, "en", store, icon_fallback="agent") != base
    assert list_signature(agents, {}, "en", store, overrides={"r": "/x.png"}) != base


# --- DeckApp.agent_list + GET /agent/list -------------------------------------

import threading  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402
from test_agent_card import _get, _serving_live, blocked, make  # noqa: E402
from test_deckapp_live import StubIcons  # noqa: E402

from herdeck.deckapp import DeckApp, MockSource  # noqa: E402
from herdeck.project_icons import default_store  # noqa: E402


def _idle(pane="p1", **kw):
    return AgentState(
        AgentKey("prod", pane), "codex", pane, Status.IDLE, repo="shop", branch="dev", workspace="Shop", **kw
    )


def test_list_carries_since_and_only_active_agents():
    app, src, runner, clock = make()
    src._on_snapshot("prod", [blocked(title="Bank API"), _idle(), _idle("p2", lifecycle="closed")])
    clock[0] += 260
    body = app.agent_list(None, 0)
    assert body["connected"] is True and body["language"] == "en"
    assert isinstance(body["version"], int)
    rows = {a["pane_id"]: a for a in body["agents"]}
    assert set(rows) == {"p0", "p1"}
    assert rows["p0"]["since_s"] == 260
    assert rows["p0"]["title"] == "Bank API" and rows["p0"]["status"] == "blocked"
    assert rows["p1"]["title"] == "Shop" and rows["p1"]["monogram"] == "S"


def test_list_marks_a_down_servers_agents_offline_and_keeps_their_status():
    app, src, runner, clock = make()
    src._on_snapshot("prod", [blocked()])
    src._on_connection("prod", False)
    body = app.agent_list(None, 0)
    assert body["connected"] is False
    assert [(a["status"], a["offline"]) for a in body["agents"]] == [("blocked", True)]


def test_list_does_not_order_rows_by_status():
    app, src, runner, clock = make()
    src._on_snapshot("prod", [_idle("a"), blocked("b"), _idle("c")])
    assert sorted(a["pane_id"] for a in app.agent_list(None, 0)["agents"]) == ["a", "b", "c"]


def test_list_long_poll_ignores_a_re_render_and_wakes_on_an_agent_change():
    app, src, runner, clock = make()
    src._on_snapshot("prod", [blocked(), _idle()])
    v = app.agent_list(None, 0)["version"]
    app.refresh()  # a frame with no row change (what an animation tick is)
    started = time.monotonic()
    same = app.agent_list(v, 200)
    assert same["version"] == v
    assert time.monotonic() - started >= 0.18

    out = {}
    waiter = threading.Thread(target=lambda: out.update(app.agent_list(v, 5000)))
    started = time.monotonic()
    waiter.start()
    time.sleep(0.05)
    src._on_snapshot("prod", [blocked(), _idle(status_since_ms=None, title="now busy")])
    waiter.join(timeout=5)
    assert out["version"] != v
    assert time.monotonic() - started < 4
    assert {a["pane_id"]: a["title"] for a in out["agents"]}["p1"] == "now busy"


def test_icon_arrival_bumps_the_list_version_and_fills_the_row():
    store = default_store()
    store.clear()
    png = _png()
    h = icon_hash(png)
    try:
        app, src, runner, clock = make()
        src._on_snapshot("prod", [blocked(project_icon=h)])
        before = app.agent_list(None, 0)
        assert before["agents"][0]["icon"] is None
        src._on_project_icon("prod", SimpleNamespace(hash=h, mime="image/png", data=png))
        after = app.agent_list(None, 0)
        assert after["agents"][0]["icon"] == h
        assert after["version"] != before["version"]
    finally:
        store.clear()


def test_mock_source_lists_the_demo_fleet():
    app = DeckApp(MockSource(), serve=False, icon_provider=StubIcons())
    try:
        body = app.agent_list(None, 0)
        assert body["agents"]
        assert all(a["offline"] is False for a in body["agents"])
    finally:
        app.close()


def test_list_route_requires_the_token_and_validates_params():
    app, src, runner = _serving_live()
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(app, "/agent/list", token="wrong")
        assert e.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(app, "/agent/list?after=x")
        assert e.value.code == 400
        code, body = _get(app, "/agent/list")
        assert code == 200
        assert [a["pane_id"] for a in body["agents"]] == ["p0"]
        code, again = _get(app, f"/agent/list?after={body['version']}&wait_ms=100")
        assert (code, again["version"]) == (200, body["version"])
    finally:
        app.close()


# --- GET /project-icon/<hash> --------------------------------------------------

import urllib.request  # noqa: E402

from herdeck.deckapp.dock_data import PROJECT_ICON_PX, project_icon_png  # noqa: E402


def _ico() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (32, 32), (30, 200, 30, 255)).save(buf, "ICO", sizes=[(32, 32)])
    return buf.getvalue()


SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16">'
    b'<rect width="16" height="16" fill="#1e64c8"/></svg>'
)


def _raw(app, path, token=None):
    url = f"http://{app.host}:{app.port}{path}?token={token if token is not None else app.token}"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


@pytest.mark.parametrize(
    "mime,data",
    [("image/png", _png()), ("image/x-icon", _ico()), ("image/svg+xml", SVG)],
    ids=["png", "ico", "svg"],
)
def test_project_icon_route_serves_a_small_png_with_a_long_cache(mime, data):
    store = default_store()
    store.clear()
    h = icon_hash(data)
    store.put(h, mime, data)
    app, src, runner = _serving_live()
    try:
        status, headers, body = _raw(app, f"/project-icon/{h}")
        assert status == 200
        assert headers["Content-Type"] == "image/png"
        assert headers["Cache-Control"] == "max-age=86400"
        with Image.open(io.BytesIO(body)) as im:
            assert im.format == "PNG"
            assert im.size == (PROJECT_ICON_PX, PROJECT_ICON_PX)
    finally:
        app.close()
        store.clear()


def test_project_icon_route_rejects_bad_tokens_malformed_and_unknown_hashes():
    store = default_store()
    store.clear()
    store.put("aaaaaaaaaaaaaaaa", "image/png", b"not an image at all")
    app, src, runner = _serving_live()
    try:
        assert _raw(app, "/project-icon/0123456789abcdef", token="wrong")[0] == 403
        for bad in ("0123456789ABCDEF", "abc", "0123456789abcdef0", "0123456789abcdef/x", ""):
            assert _raw(app, f"/project-icon/{bad}")[0] == 400, bad
        assert _raw(app, "/project-icon/0123456789abcdef")[0] == 404  # unknown
        assert _raw(app, "/project-icon/aaaaaaaaaaaaaaaa")[0] == 404  # undecodable
    finally:
        app.close()
        store.clear()


def test_project_icon_png_never_echoes_the_stored_bytes():
    store = ProjectIconStore()
    h = icon_hash(SVG)
    store.put(h, "image/svg+xml", SVG)
    code, png = project_icon_png(h, store)
    assert code == 200
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert SVG not in png


# --- agent-logo fallback: GET /agent-mark/<type> ------------------------------


class _MarkIcons(StubIcons):
    def __init__(self):
        self.calls = []

    def agent_mark_image(self, agent_type, size):
        self.calls.append((agent_type, size))
        if agent_type == "boom":
            raise RuntimeError("glyph failed")
        return Image.new("RGBA", (size, size), (255, 255, 255, 255))


def test_agent_mark_png_is_a_square_png_and_rejects_bad_types():
    icons = _MarkIcons()
    code, png = agent_mark_png("claude", icons)
    assert code == 200
    with Image.open(io.BytesIO(png)) as im:
        assert im.format == "PNG" and im.size == (PROJECT_ICON_PX,) * 2
    assert icons.calls == [("claude", PROJECT_ICON_PX)]
    for bad in ("../etc", "", "Claude!", "a" * 33, "claude\n"):
        assert agent_mark_png(bad, icons) == (400, None), bad
    assert agent_mark_png("boom", icons) == (404, None)


def test_agent_list_reports_the_icon_fallback_and_wakes_when_it_changes():
    app = DeckApp(MockSource(), serve=False, icon_provider=_MarkIcons())
    try:
        first = app.agent_list(None, 0)
        assert first["icon_fallback"] == "monogram"
        app._source.config.view.project_icon_fallback = "agent"
        app.refresh()
        second = app.agent_list(None, 0)
        assert second["icon_fallback"] == "agent"
        assert second["version"] != first["version"]
        app._source.config.view.project_icon_fallback = "bogus"
        assert app.agent_list(None, 0)["icon_fallback"] == "monogram"
    finally:
        app.close()


def test_agent_mark_route_requires_the_token_validates_and_caches():
    app = DeckApp(MockSource(), host="127.0.0.1", port=0, serve=True, icon_provider=_MarkIcons())
    try:
        assert _raw(app, "/agent-mark/claude", token="wrong")[0] == 403
        assert _raw(app, "/agent-mark/Claude!")[0] == 400
        assert _raw(app, "/agent-mark/")[0] == 400
        status, headers, body = _raw(app, "/agent-mark/claude")
        assert status == 200
        assert headers["Content-Type"] == "image/png"
        assert headers["Cache-Control"] == "max-age=86400"
        assert body.startswith(b"\x89PNG")
        assert _raw(app, "/agent-mark/boom")[0] == 404
    finally:
        app.close()
