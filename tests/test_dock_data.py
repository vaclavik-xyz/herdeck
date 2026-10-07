"""The desktop mini dock's data: GET /agent/list and GET /project-icon/<hash>."""

import io

from PIL import Image

from herdeck.deckapp.dock_data import agent_rows, list_signature, monogram, row_title
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
