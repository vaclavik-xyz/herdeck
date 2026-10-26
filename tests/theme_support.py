"""Shared fixtures for the built-in deck theme tests (signal, headline, quiet).

The ONE place that knows how the platform (plan A1) builds a ThemeContext and
an IconProvider: if A1's constructor differs, align it here only.

``TextLog`` records every ``ImageDraw.text`` call a theme makes, so tests can
check the D200 legibility floor (>= 22 px), that text stays inside the image /
the status frame, never overlaps other text, and never straddles the Elgato
seam at x = 196 of a 392 px panel.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

from PIL import ImageDraw

from herdeck import icons
from herdeck.config import DEFAULT_STATUS_COLORS
from herdeck.driver.base import PanelAgent, PanelGauge, PanelView, ThemeEnv, TileView
from herdeck.frozen import make_png_rasterizer
from herdeck.project_icons import ProjectIconStore
from herdeck.themes.api import ThemeContext

# Only text drawn by the built-in theme modules counts: the platform's own
# mark helpers (a monogram letter, an unknown agent's letter glyph) are not
# theme text.
THEME_MODULES = frozenset(
    f"herdeck.themes.{name}" for name in ("_kit", "_signal_panel", "signal", "headline", "quiet")
)

STATUSES = ["blocked", "working", "idle", "done", "waiting", "offline", "unknown", None]
WIDTHS = [458, 392]
LANGS = ["en", "cs"]
_COLOR = {
    "blocked": "amber",
    "working": "green",
    "idle": "blue",
    "done": "cyan",
    "waiting": "violet",
    "offline": "red",
    "unknown": "grey",
    None: "dim",
}


def make_provider(tmp_path) -> icons.IconProvider:
    # The frozen path's rasterizer: bundled marks come from the committed
    # pre-baked PNGs, so renders are identical on macOS and Linux CI.
    return icons.IconProvider(
        cache_dir=str(tmp_path / "cache"),
        slug_map={},
        fetch=lambda slug: None,
        rasterize=make_png_rasterizer(icons._ASSETS_DIR),
        project_icons=ProjectIconStore(),
    )


def env(language: str = "en", colors: dict[str, str] | None = None) -> ThemeEnv:
    """What layout.theme_env(config) stamps: the language + the full
    [theme.colors] map (defaults overlaid with ``colors``)."""
    merged = {**DEFAULT_STATUS_COLORS, **(colors or {})}
    return ThemeEnv(language, tuple(sorted(merged.items())))


def make_ctx(provider, language: str = "en", colors: dict[str, str] | None = None) -> ThemeContext:
    return ThemeContext(env(language, colors), provider)


def agent(status, **kw) -> TileView:
    """An agent tile as layout.agent_tile_view builds it (plus A1's fields)."""
    word = {None: "PENDING"}.get(status, (status or "").upper())
    base = dict(
        agent_type="claude",
        repo="herdeck",
        branch="feat/deck-themes",
        status_text=word,
        time_text="12m",
        status=status,
        wait_reason="CI" if status == "waiting" else "",
        kind="agent",
        title="Fix the flaky render golden test",
        workspace="herdeck",
    )
    color = kw.pop("color", _COLOR[status])
    base.update(kw)
    # Ruling P1: agent tiles carry project data by default, so the project mark
    # (favicon/monogram) is what the built-in themes show; drop it with
    # project_name="" / repo="" to exercise the agent-logo path.
    base.setdefault("project_name", base["repo"])
    return TileView(0, "herdeck", color, **base)


def agent_variants() -> dict[str, TileView]:
    out: dict[str, TileView] = {}
    for st in STATUSES:
        out[f"{st}"] = agent(st)
        out[f"{st}_project_subagents"] = agent(
            st, tile_icon="project", project_name="herdeck", subagents=3, title=""
        )
    out["long_title"] = agent(
        "working", title="A very long task title that has to wrap over two lines and more"
    )
    out["no_title_no_workspace"] = agent("idle", title="", workspace="")
    out["no_agent_type"] = agent(None, agent_type=None, repo="pending-thing")
    out["waiting_long_label"] = agent("waiting", wait_reason="REVIEW +1", subagents=2)
    return out


def key(label, color, kind, **kw) -> TileView:
    return TileView(0, label, color, kind=kind, **kw)


def key_variants() -> dict[str, TileView]:
    return {
        "option_digit": key(
            "1", "green", "option",
            subtext="Yes, and don't ask again for rm commands in /home/user/projects",
        ),
        "option_short": key("3", "red", "option", subtext="No"),
        "option_amber": key("2", "amber", "option", subtext="Yes, allow all edits this session"),
        "option_label_only": key("Approve", "green", "option"),
        "option_sure": key("Sure?", "red", "option"),
        "back": key("Back", "grey", "control"),
        "stop": key("Stop", "red", "control"),
        "stop_unavailable": key("Stop", "grey", "control"),
        "pin": key("Pin here", "grey", "control"),
        "management": key("Notify", "grey", "control"),
        "menu_profile": key("* default", "blue", "menu"),
        "new_slot": key("+ New", "launcher", "launcher"),
        "new_slot_control_kind": key("+ New", "launcher", "control"),
        # the New-agent menu: A1 builds its entries with kind="menu"
        "launcher_agent": key("claude", "blue", "menu", agent_type="claude"),
        "launcher_unknown_agent": key("zzagent", "blue", "menu", agent_type="zzagent"),
        "launcher_profiles": key("Profiles", "blue", "menu"),
        "idle_group": key("+12", "grey", "idle_group", subtext="idle · show"),
        "idle_group_hide": key("hide", "grey", "idle_group", subtext="idle"),
        "pinned_hole": key("Pinned · missing", "grey", "pinned_hole", subtext="Unpin"),
        "empty": key("", "empty", "empty"),
    }


def _pa(label, elapsed, *, project="", agent_type="claude", title="task") -> PanelAgent:
    return PanelAgent(label, title, elapsed, agent_type, None, project)


def panel_variants() -> dict[str, PanelView]:
    def pv(kind, title="", **kw):
        queue = kw.pop("queue", [])
        counts = kw.pop("counts", {})
        return PanelView(title, kind=kind, queue=queue, counts=counts, **kw)

    queue = [
        _pa("lumen", "4m", project="lumen"),
        _pa("orbit", "1m"),
        _pa("atlas", "30s", project="atlas"),
        _pa("ember", "10s", project="ember"),
    ]
    gauges = [
        PanelGauge("Claude", "5H", 72, color="green"),
        PanelGauge("Claude", "7D", 40, color="green"),
        PanelGauge("Codex", "5H", 91, color="red"),
    ]
    return {
        "spotlight_queue_note": pv(
            "spotlight", "4 need you", headline="lumen", color="amber", solid=True,
            meta="longest 4m", note="laptop offline", queue=queue,
            counts={"blocked": 4, "working": 2, "offline": 3},
        ),
        "spotlight_one": pv(
            "spotlight", "Needs you", headline="lumen", color="amber", solid=True, meta="4m",
            queue=queue[:1], counts={"blocked": 1},
        ),
        "spotlight_many_no_note": pv(
            "spotlight", "4 need you", headline="lumen", color="amber", solid=True,
            meta="longest 4m", queue=queue, counts={"blocked": 4},
        ),
        "spotlight_no_queue": pv(
            "spotlight", "Needs you", headline="lumen", color="amber", solid=True, meta="4m",
        ),
        "calm_usage_paged": pv(
            "calm", "All clear", page=(0, 3), gauges=gauges,
            counts={"working": 9, "waiting": 1, "done": 2, "idle": 7, "offline": 3},
        ),
        "calm_note": pv(
            "calm", "All clear", note="t3 offline", counts={"working": 2, "idle": 12, "offline": 1},
        ),
        "calm_many_pages": pv(
            "calm", "All clear", page=(4, 9), counts={"working": 120, "idle": 999},
        ),
        "calm_no_agents": pv("calm", "No agents"),
        "offline": pv(
            "offline", "Offline", headline="Reconnecting…", lines=["macbench"], color="red",
            solid=True, meta="2m", counts={"offline": 5}, queue=queue[:2],
        ),
        "config_error": pv(
            "config_error", "Config error", headline="No token for macbench",
            lines=["Fix it in the app → Maintenance"], color="red", solid=True,
            hint="the deck recovers once it's fixed",
        ),
        "detail_blocked_short": pv(
            "detail", "BLOCKED · 4m", color="amber", meta="claude · herdeck",
            hint="answer on the keys",
            lines=["Do you want to make this edit to src/herdeck/themes/signal.py?"],
        ),
        "detail_long": pv(
            "detail", "WORKING · 1m", color="green", meta="claude · herdeck",
            headline="Running tests",
            lines=["Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod"
                   " tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim"
                   " veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip."],
        ),
        "usage": pv(
            "usage", "Usage limits", meta="used / reset", page=(0, 2), hint="press · more",
            gauges=[
                PanelGauge("Session", "5h", 42, hint="reset 14:09", color="green",
                           pace="full ~40m early"),
                PanelGauge("Weekly", "7d", 87, hint="reset Mon", color="amber"),
                PanelGauge("Codex", "5H", 10, color="green"),
            ],
        ),
        "menu": pv("menu", "New agent", headline="Pick a type", lines=["on macbench"]),
        "sent": pv("sent", "Needs you", sent="Sent to herdeck", meta="1 left", color="amber"),
        "spotlight_sent": pv(
            "spotlight", "Needs you", headline="lumen", color="amber", solid=True,
            sent="Sent to lumen", meta="1 left", queue=queue[:2],
        ),
    }


@dataclass
class TextLog:
    """Every ``ImageDraw.text`` call: (x0, y0, x1, y1) ink box, text, size."""

    entries: list[tuple[tuple[float, float, float, float], str, int]] = field(default_factory=list)

    def install(self, monkeypatch) -> TextLog:
        original = ImageDraw.ImageDraw.text
        log = self

        def text(draw, xy, text, *args, **kwargs):
            caller = sys._getframe(1).f_globals.get("__name__", "")
            if caller not in THEME_MODULES:
                return original(draw, xy, text, *args, **kwargs)
            font = kwargs.get("font")
            assert font is not None, f"text {text!r} drawn without an explicit font"
            assert "anchor" not in kwargs, "built-in themes draw text top-left (no anchor)"
            x0, y0, x1, y1 = font.getbbox(text)
            log.entries.append(((xy[0] + x0, xy[1] + y0, xy[0] + x1, xy[1] + y1), text, font.size))
            return original(draw, xy, text, *args, **kwargs)

        monkeypatch.setattr(ImageDraw.ImageDraw, "text", text)
        return self

    def take(self) -> list[tuple[tuple[float, float, float, float], str, int]]:
        out, self.entries = self.entries, []
        return [e for e in out if e[1].strip()]


def overlaps(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def assert_text_sane(entries, size, *, inside=None, seam=None, name=""):
    """Floor, bounds, no overlap (and optionally: inside a box, off the seam)."""
    w, h = size
    for box, text, px in entries:
        assert px >= 22, f"{name}: {text!r} at {px}px (< 22px floor)"
        assert box[0] >= 0 and box[1] >= 0 and box[2] <= w and box[3] <= h, (
            f"{name}: {text!r} leaves the image: {box}"
        )
        if inside is not None:
            assert (
                box[0] >= inside[0] and box[1] >= inside[1]
                and box[2] <= inside[2] and box[3] <= inside[3]
            ), f"{name}: {text!r} crosses the status frame: {box}"
        if seam is not None:
            assert not (box[0] < seam < box[2]), f"{name}: {text!r} straddles x={seam}: {box}"
    for i, (a, ta, _) in enumerate(entries):
        for b, tb, _ in entries[i + 1:]:
            assert not overlaps(a, b), f"{name}: {ta!r} overlaps {tb!r}"
