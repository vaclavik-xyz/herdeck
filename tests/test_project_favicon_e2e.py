"""Repo favicon -> bridge discovery -> wire frame -> connector -> store ->
orchestrator -> rendered tile PNG, with a stub herdr and a real websocket."""

import asyncio
import contextlib
import io

from PIL import Image

from herdeck.bridge import StubHerdr, start_local_bridge
from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig
from herdeck.connector import Connector
from herdeck.icons import IconProvider
from herdeck.orchestrator import Orchestrator
from herdeck.project_icon_discovery import icon_hash
from herdeck.project_icons import ProjectIconStore, default_store

RED = (220, 20, 20)


def _favicon() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (32, 32), RED + (255,)).save(buf, "PNG")
    return buf.getvalue()


def _white(svg, size):
    return Image.new("RGBA", (size, size), (255, 255, 255, 255))


async def test_project_favicon_flows_from_repo_to_rendered_tile(tmp_path):
    favicon = _favicon()
    repo = tmp_path / "shop"
    (repo / ".git").mkdir(parents=True)
    (repo / "public").mkdir()
    (repo / "public" / "favicon.png").write_bytes(favicon)
    herdr = StubHerdr(
        panes=[
            {
                "pane_id": "w1:p1",
                "workspace_id": "w1",
                "cwd": str(repo / "src"),
                "foreground_cwd": str(repo / "src"),
                "agent": "claude",
                "agent_status": "idle",
            }
        ]
    )
    host, port, token, (server, btask) = await start_local_bridge("unused.sock", herdr=herdr)
    store = ProjectIconStore()
    states: list = []
    arrived = asyncio.Event()

    def on_icon(sid, icon):
        store.put(icon.hash, icon.mime, icon.data)
        arrived.set()

    server_cfg = ServerConfig("local", f"ws://{host}:{port}", token)
    conn = Connector(
        server_cfg,
        on_snapshot=lambda sid, st: states.__setitem__(slice(None), st),
        on_event=lambda sid, s: None,
        on_connection=lambda sid, up: None,
        on_project_icon=on_icon,
    )
    task = asyncio.create_task(conn.run())
    try:
        await asyncio.wait_for(arrived.wait(), timeout=5)
    finally:
        conn.stop()
        await asyncio.wait_for(task, timeout=3)
        btask.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await btask
        server.close()
        await server.wait_closed()

    h = icon_hash(favicon)
    assert states and states[0].project_icon == h

    def render(mode: str) -> bytes:
        cfg = Config(
            servers=[server_cfg],
            profiles=dict(DEFAULT_PROFILES),
            overview_order=["local"],
            grid=(5, 3),
        )
        cfg.view.tile_icon = mode
        orch = Orchestrator(cfg, slots=13, project_icons=store)
        orch.apply_snapshot("local", states)
        tile = orch.render().tiles[0]
        provider = IconProvider(
            cache_dir=str(tmp_path / f"cache-{mode}"),
            slug_map={"claude": None},
            fetch=lambda s: None,
            rasterize=_white,
            assets_dir=None,
            project_icons=store,
        )
        return provider.render_tile_bytes(tile)

    agent, project = render("agent"), render("project")
    assert agent != project
    pixel = Image.open(io.BytesIO(project)).convert("RGB").getpixel((35, 35))
    assert all(abs(a - b) <= 4 for a, b in zip(pixel, RED, strict=True))
    # Every hop used the injected store; the process-wide one stays untouched.
    assert h not in default_store()
