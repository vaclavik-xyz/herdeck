"""Render a synthetic fleet with Herdeck's real tile and panel renderer.

Run from the repository root in a Python environment with the `dev` extra:
    PYTHONPATH=src python website/scripts/render-agent-reference.py

This creates UI artwork only. It never connects to agents or the USB device.
"""

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from herdeck.driver.base import PanelView, TileView
from herdeck.frozen import make_png_rasterizer
from herdeck.icons import _ASSETS_DIR, IconProvider, compose_panel
from herdeck.project_icons import ProjectIconStore

# Synthetic projects, no user session names or private runtime data.
FLEET = [
    ("claude", "herdeck", "feat/website", "BLOCKED", "amber"),
    ("codex", "api-server", "feat/search", "WORKING", "green"),
    ("gemini", "docs", "main", "DONE", "cyan"),
    ("cursor", "web-app", "fix/menu", "WORKING", "green"),
    ("opencode", "design", "main", "IDLE", "blue"),
    ("hermes", "pipeline", "feat/build", "WORKING", "green"),
    ("claude", "auth", "fix/login", "WORKING", "green"),
    ("codex", "checkout", "feat/cart", "IDLE", "blue"),
    ("gemini", "search", "main", "DONE", "cyan"),
    ("cursor", "dashboard", "feat/stats", "WORKING", "green"),
    ("opencode", "ui-kit", "main", "IDLE", "blue"),
    ("hermes", "tests", "fix/spec", "WORKING", "green"),
    ("claude", "mobile", "feat/sync", "WORKING", "green"),
]


def main():
    target = Path(__file__).resolve().parents[1] / "design" / "agent-ui-reference.png"
    target.parent.mkdir(exist_ok=True)
    sheet = Image.new("RGB", (1204, 732), (12, 16, 23))
    with TemporaryDirectory(prefix="herdeck-marketing-ui-") as cache:
        provider = IconProvider(
            cache_dir=cache,
            slug_map={},
            fetch=lambda _: None,
            rasterize=make_png_rasterizer(_ASSETS_DIR),
            project_icons=ProjectIconStore(),
        )
        for index, (agent, repo, branch, status, color) in enumerate(FLEET):
            tile = TileView(
                index=index,
                label=repo,
                color=color,
                agent_type=agent,
                repo=repo,
                branch=branch,
                status_text=status,
                time_text="2m",
                tile_fill="tint",
                tile_icon="agent",
                working_animation="none",
            )
            rendered = Image.open(BytesIO(provider.render_tile_bytes(tile)))
            sheet.paste(rendered, (32 + (index % 5) * 236, 32 + (index // 5) * 236))
        panel = compose_panel(
            PanelView(
                title="Needs you",
                headline="herdeck · claude",
                meta="13 agents",
                lines=["Run the project test suite?"],
                hint="press · answer",
                color="amber",
                solid=True,
            )
        )
        sheet.paste(panel, (32 + 3 * 236, 32 + 2 * 236))
    sheet.save(target)
    print(target)


if __name__ == "__main__":
    main()
