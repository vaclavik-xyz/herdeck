"""Capture the real deck's behaviour as a replayable graph for the website demo.

Run from the repository root with the dev environment:
    TZ=Europe/Prague PYTHONPATH=src .venv/bin/python website/scripts/capture-deck.py

Herdeck's own Orchestrator (press logic, ordering, drills, triage, confirms,
usage paging) and its own tile/panel renderer run headless against a fictional
fleet. Every view reachable by pressing keys is rendered once; the website
replays the recorded transitions. Nothing connects to agents or a device.

Output (per language):
    website/public/deck/<lang>/graph.json   views, press edges and timed edges
    website/public/deck/<lang>/<hash>.webp  every distinct tile/panel image
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import shutil
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageDraw

from herdeck import layout
from herdeck.config import DEFAULT_PROFILES, Config, ServerConfig
from herdeck.deckapp.server import tile_accessible_label
from herdeck.frozen import make_png_rasterizer
from herdeck.i18n import tr
from herdeck.icons import _ASSETS_DIR, PANEL_W, IconProvider, compose_panel
from herdeck.model import AgentKey, AgentState, Status
from herdeck.orchestrator import Orchestrator
from herdeck.project_icon_discovery import icon_hash
from herdeck.project_icons import ProjectIconStore
from herdeck.usage import ProviderUsage, UsageWindow

OUT = Path(__file__).resolve().parents[1] / "public" / "deck"
LANGS = ("en", "cs")
KEYS = 13  # 5 + 5 + 3 keys; the status window is press index 13
PANEL = 13
PRESS_GAP_S = 0.35  # > the 0.3s slot guard, like a person pressing
TICK_S = 0.4  # the runtime tick: timed changes are probed on this grid
PROBE_LIMIT_S = 8.0  # sent note 3s, settle 2s, confirm 5s, usage hold 6s
MAX_VIEWS = 4000  # a safety net, far above what the scenarios reach
NOW = datetime(2026, 10, 6, 14, 20, tzinfo=UTC)

# ---------------------------------------------------------------- fictional data
# Invented projects with invented favicons. "ledger" deliberately has none, so
# the deck falls back to its monogram, as it does for a repo without a favicon.
FAVICONS = {
    "lumen": ("#f5b82e", "sun"),
    "orbit": ("#6c5ce7", "orbit"),
    "harbor": ("#1f8ef1", "wave"),
    "quill": ("#f0eadc", "quill"),
    "atlas": ("#10b981", "globe"),
    "ember": ("#ef5a3c", "flame"),
    "nimbus": ("#7dd3fc", "cloud"),
    "pixelkit": ("#ec4899", "pixels"),
    "tidepool": ("#0ea5a4", "drop"),
    "beacon": ("#facc15", "beacon"),
    "sprout": ("#84cc16", "leaf"),
    "fern": ("#22c55e", "leaf"),
    "kiln": ("#f97316", "flame"),
}

CLAUDE_PROMPT = (
    "Run the integration tests?\n"
    "❯ 1. Yes\n"
    "  2. Yes, and don't ask again for npm test\n"
    "  3. No, and tell Claude what to do differently"
)
CODEX_PROMPT = (
    "Allow command: pnpm prisma migrate deploy\n❯ 1. Yes, proceed\n  2. No, provide feedback"
)


def agent(server, pane, kind, repo, status, branch, age, **extra) -> tuple[AgentState, int]:
    """One fictional agent and how long it has held its status (seconds)."""
    state = AgentState(
        AgentKey(server, pane), kind, repo, status, repo=repo, branch=branch, **extra
    )
    return state, age


def main_fleet():
    return [
        agent("studio", "p1", "claude", "lumen", Status.BLOCKED, "feat/checkout", 260),
        agent("laptop", "p2", "codex", "orbit", Status.BLOCKED, "db/migrate", 70),
        agent(
            "studio",
            "p3",
            "claude",
            "harbor",
            Status.WAITING,
            "release/2.4",
            540,
            waiting_on="⏳ ci",
        ),
        agent("studio", "p4", "gemini", "quill", Status.DONE, "docs/api", 120),
        agent(
            "laptop",
            "p5",
            "cursor",
            "atlas",
            Status.WORKING,
            "feat/search",
            720,
            subagents_running=2,
            subagents_total=3,
        ),
        agent(
            "studio",
            "p6",
            "claude",
            "ember",
            Status.WORKING,
            "fix/cart-total",
            380,
            progress="Running 214 tests",
        ),
        agent("laptop", "p7", "codex", "nimbus", Status.WORKING, "perf/cache", 95),
        agent("studio", "p8", "opencode", "pixelkit", Status.WORKING, "feat/tokens", 1500),
        agent("laptop", "p9", "claude", "tidepool", Status.WORKING, "main", 40),
        agent("studio", "p10", "codex", "ledger", Status.IDLE, "main", 2400),
        agent("laptop", "p11", "hermes", "beacon", Status.IDLE, "feat/alerts", 5400),
        agent("studio", "p12", "gemini", "sprout", Status.DONE, "chore/deps", 30),
    ]


def big_fleet():
    calm = [a for a in main_fleet() if a[0].status is not Status.BLOCKED]
    extra = [
        agent("laptop", "q1", "claude", "fern", Status.WORKING, "feat/onboarding", 300),
        agent("studio", "q2", "codex", "kiln", Status.WORKING, "fix/flaky-test", 150),
        agent("laptop", "q3", "cursor", "relay", Status.IDLE, "main", 3600),
        agent("studio", "q4", "gemini", "mosaic", Status.IDLE, "main", 7200),
        agent("laptop", "q5", "opencode", "vault", Status.IDLE, "main", 900),
        agent("studio", "q6", "claude", "compass", Status.WORKING, "feat/geo", 600),
        agent("laptop", "q7", "codex", "signal", Status.IDLE, "main", 1800),
        agent("studio", "q8", "hermes", "loom", Status.WORKING, "feat/weave", 200),
        agent("laptop", "q9", "claude", "prism", Status.IDLE, "main", 4000),
    ]
    return calm + extra


SCENARIOS = {
    # The everyday case: two agents need you; answer them, then the calm deck.
    "needs-you": dict(fleet=main_fleet, down=()),
    # A fleet larger than the keys: idle agents fold into one tile, pages.
    "big-fleet": dict(fleet=big_fleet, down=(), collapse_idle=True),
    # One machine dropped off: its agents turn red, the rest keep working.
    "outage": dict(fleet=main_fleet, down=("laptop",)),
    # Every machine is unreachable.
    "offline": dict(fleet=main_fleet, down=("studio", "laptop")),
}

USAGE = [
    ProviderUsage(
        "claude",
        [
            UsageWindow(
                "5h", 72, (NOW + timedelta(hours=2, minutes=10)).isoformat(), full_early_s=40 * 60
            ),
            UsageWindow("week", 38, (NOW + timedelta(days=3)).isoformat()),
        ],
        "paid",
    ),
    ProviderUsage(
        "codex",
        [
            UsageWindow("5h", 91, (NOW + timedelta(minutes=50)).isoformat(), full_early_s=25 * 60),
            UsageWindow("week", 54, (NOW + timedelta(days=5)).isoformat()),
        ],
        "paid",
    ),
]


def draw_favicon(color: str, shape: str) -> bytes:
    """A small, invented project favicon: a glyph on a rounded colour plate."""
    s = 128
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rgb = tuple(int(color[i : i + 2], 16) for i in (1, 3, 5))
    d.rounded_rectangle((0, 0, s - 1, s - 1), 30, fill=rgb)
    lum = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
    ink = (20, 22, 28) if lum > 150 else (255, 255, 255)
    c = s // 2
    if shape == "sun":
        d.ellipse((c - 22, c - 22, c + 22, c + 22), fill=ink)
        for k in range(8):
            a = k * math.pi / 4
            d.line(
                (
                    c + 32 * math.cos(a),
                    c + 32 * math.sin(a),
                    c + 46 * math.cos(a),
                    c + 46 * math.sin(a),
                ),
                fill=ink,
                width=8,
            )
    elif shape == "orbit":
        d.ellipse((c - 18, c - 18, c + 18, c + 18), fill=ink)
        d.ellipse((c - 48, c - 20, c + 48, c + 20), outline=ink, width=7)
    elif shape == "wave":
        for y in (c - 18, c + 6):
            d.arc((c - 46, y - 14, c, y + 14), 180, 360, fill=ink, width=8)
            d.arc((c, y - 14, c + 46, y + 14), 0, 180, fill=ink, width=8)
    elif shape == "quill":
        d.polygon([(c + 34, c - 44), (c - 30, c + 28), (c - 14, c + 34)], fill=ink)
        d.line((c - 26, c + 30, c - 40, c + 46), fill=ink, width=7)
    elif shape == "globe":
        d.ellipse((c - 42, c - 42, c + 42, c + 42), outline=ink, width=7)
        d.ellipse((c - 18, c - 42, c + 18, c + 42), outline=ink, width=6)
        d.line((c - 42, c, c + 42, c), fill=ink, width=6)
    elif shape == "flame":
        d.ellipse((c - 30, c - 6, c + 30, c + 46), fill=ink)
        d.polygon([(c, c - 50), (c - 29, c + 14), (c + 29, c + 14)], fill=ink)
    elif shape == "cloud":
        for box in (
            (c - 44, c - 4, c - 4, c + 34),
            (c - 22, c - 30, c + 22, c + 14),
            (c + 2, c - 12, c + 44, c + 34),
        ):
            d.ellipse(box, fill=ink)
        d.rectangle((c - 26, c + 10, c + 26, c + 34), fill=ink)
    elif shape == "pixels":
        for i in range(3):
            for j in range(3):
                if (i + j) % 2 == 0:
                    d.rectangle(
                        (c - 42 + i * 28, c - 42 + j * 28, c - 20 + i * 28, c - 20 + j * 28),
                        fill=ink,
                    )
    elif shape == "drop":
        d.ellipse((c - 30, c - 8, c + 30, c + 46), fill=ink)
        d.polygon([(c, c - 48), (c - 28, c + 12), (c + 28, c + 12)], fill=ink)
    elif shape == "beacon":
        d.polygon(
            [(c - 16, c - 20), (c + 16, c - 20), (c + 26, c + 44), (c - 26, c + 44)], fill=ink
        )
        d.rectangle((c - 22, c - 44, c + 22, c - 26), fill=ink)
    elif shape == "leaf":
        d.ellipse((c - 40, c - 26, c + 40, c + 26), fill=ink)
        d.line((c - 46, c + 30, c + 10, c - 4), fill=rgb, width=7)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


# ---------------------------------------------------------------- simulation
class Clock:
    """A settable clock that deep-copies with the orchestrator it drives."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class DemoOrchestrator(Orchestrator):
    """The real Orchestrator with time frozen for display: tiles keep their
    invented ages instead of counting up while someone clicks around."""

    ages: dict[AgentKey, tuple[Status, int]]

    def _elapsed_text(self, key: AgentKey) -> str:
        agent = self._agents.get(key)
        start = self.ages.get(key)
        if agent is None:
            return ""
        if start and start[0] is agent.status:
            return self._fmt_elapsed(start[1])
        return "0s"  # its status just changed in the demo

    def _down_for(self) -> str:
        return "2m" if self._down else ""


class DemoWorld:
    """Plays the bridge and herdr: applies the commands a press produces the way
    a live agent would answer them."""

    def __init__(self, orch: DemoOrchestrator, prompts: dict[AgentKey, str]) -> None:
        self.orch = orch
        self.prompts = prompts
        self.pending_read: AgentKey | None = None

    def _set_status(self, key: AgentKey, status: Status) -> None:
        current = self.orch.get_agent(key)
        if current is not None and current.status is not status:
            self.orch.apply_event(key.server_id, replace(current, status=status, progress=""))

    def run(self, commands) -> None:
        for cmd in commands:
            key = AgentKey(cmd.server_id, cmd.pane_id) if cmd.pane_id else None
            agent = self.orch.get_agent(key) if key else None
            if cmd.kind == "read" and key:
                # The prompt arrives a moment after the drill opens.
                self.pending_read = key
            elif cmd.kind == "focus" and agent and agent.status is Status.DONE:
                self._set_status(key, Status.IDLE)  # herdr marks a seen pane idle
            elif cmd.kind in ("act_if_blocked", "choose_if_blocked") and key:
                self._set_status(key, Status.WORKING)
            # Stop and macros play out on the deck (confirm, "Sent to ...") but
            # leave the agent as it was: every lasting change multiplies the
            # views to capture, and these would only ever add idle tiles.

    def finish_read(self) -> bool:
        if self.pending_read is None:
            return False
        key, self.pending_read = self.pending_read, None
        if self.orch.drill_key() == key:
            agent = self.orch.get_agent(key)
            blocked = agent is not None and agent.status is Status.BLOCKED
            self.orch.set_detection(self.prompts.get(key, "") if blocked else "")
        return True


def _frozen_reset(resets_at, now=None, _format=layout._fmt_reset):
    return _format(resets_at, NOW)


# Usage reset times are formatted against "now"; freeze it with the fleet.
layout._fmt_reset = _frozen_reset


def build_config(lang: str, collapse_idle: bool) -> Config:
    cfg = Config(
        servers=[ServerConfig("studio", "", ""), ServerConfig("laptop", "", "")],
        profiles=dict(DEFAULT_PROFILES),
        overview_order=["studio", "laptop"],
        grid=(5, 3),
    )
    cfg.view.language = lang
    cfg.view.tile_fill = "solid"
    # Still tiles: the deck's 8-frame working animation reads as choppy on a web
    # page, and "none" is a real setting, so the demo stays truthful.
    cfg.view.working_animation = "none"
    cfg.view.tile_icon = "project"
    cfg.view.collapse_idle = collapse_idle
    return cfg


def start_world(name: str, lang: str, store: ProjectIconStore) -> DemoWorld:
    spec = SCENARIOS[name]
    cfg = build_config(lang, spec.get("collapse_idle", False))
    clock = Clock()
    orch = DemoOrchestrator(cfg, slots=KEYS, clock=clock, project_icons=store)
    fleet = spec["fleet"]()
    orch.ages = {}
    prompts = {}
    by_server: dict[str, list[AgentState]] = {}
    for state, age in fleet:
        fav = FAVICONS.get(state.repo)
        if fav:
            data = draw_favicon(*fav)
            digest = icon_hash(data)
            store.put(digest, "image/png", data)
            state = replace(state, project_icon=digest)
        orch.ages[state.key] = (state.status, age)
        by_server.setdefault(state.key.server_id, []).append(state)
        if state.status is Status.BLOCKED:
            prompts[state.key] = CLAUDE_PROMPT if state.agent_type == "claude" else CODEX_PROMPT
    # Older blocks first, so triage visits the longest waiting agent first.
    for sid in cfg.overview_order:
        orch.apply_snapshot(sid, by_server.get(sid, []))
        orch.set_connection(sid, True)
    for blocked_key, (_, age) in sorted(
        ((k, v) for k, v in orch.ages.items() if v[0] is Status.BLOCKED), key=lambda kv: -kv[1][1]
    ):
        orch._since[blocked_key] = (Status.BLOCKED, clock.now - age, None)
    for sid in spec["down"]:
        orch.set_connection(sid, False)
    orch.set_usage(USAGE)
    clock.now += 5.0  # past the initial order settle
    orch.render()
    return DemoWorld(orch, prompts)


def fork(world: DemoWorld, store: ProjectIconStore) -> DemoWorld:
    return copy.deepcopy(world, memo={id(store): store})


# ---------------------------------------------------------------- rendering
class Renderer:
    def __init__(self, lang: str, store: ProjectIconStore, cache: str, out: Path) -> None:
        self.lang = lang
        self.out = out
        self.icons = IconProvider(
            cache_dir=cache,
            slug_map={},
            fetch=lambda _: None,
            rasterize=make_png_rasterizer(_ASSETS_DIR),
            project_icons=store,
        )
        self.images: dict[str, str] = {}  # content digest -> file stem

    def save(self, png: bytes, quality: int) -> str:
        digest = hashlib.sha1(png).hexdigest()[:12]
        if digest not in self.images:
            img = Image.open(io.BytesIO(png)).convert("RGB")
            img.save(self.out / f"{digest}.webp", "WEBP", quality=quality, method=6)
            self.images[digest] = digest
        return digest

    def tile(self, tile) -> str:
        return self.save(self.icons.render_tile_bytes(tile), 86)

    def panel(self, panel) -> str:
        out = io.BytesIO()
        compose_panel(panel, width=PANEL_W).save(out, "PNG")
        return self.save(out.getvalue(), 88)


def panel_label(panel, lang: str) -> str:
    parts = [panel.title, panel.headline, *(panel.lines or []), panel.meta, panel.hint]
    return " · ".join(str(p).strip() for p in parts if p and str(p).strip()) or tr(
        lang, "panel.all_clear"
    )


def snapshot(world: DemoWorld, renderer: Renderer):
    """Render the current view: 13 tiles + panel."""
    orch = world.orch
    base = fork(world, renderer.icons._project_icons).orch
    state = base.render()
    tiles = []
    labels = []
    for tile in state.tiles[:KEYS]:
        labels.append(tile_accessible_label(tile, renderer.lang))
        tiles.append(renderer.tile(tile))
    panel = renderer.panel(state.panel)
    labels.append(panel_label(state.panel, renderer.lang))
    view = {"t": tiles, "p": panel, "a": labels}
    fleet = sorted((k.server_id, k.pane_id, a.status.value) for k, a in orch._agents.items())
    # A view is what the deck shows plus the little hidden state that changes
    # what the next press does. Bookkeeping that never shows (an expired note,
    # the last usage page) would otherwise split one view into hundreds.
    key = json.dumps([tiles, panel, fleet, orch._triage, world.pending_read is not None])
    return hashlib.sha1(key.encode()).hexdigest(), view


def advance(world: DemoWorld, seconds: float) -> None:
    world.orch._clock.now += seconds
    world.orch.consume_expired_panel_hold()


def explore(lang: str, store: ProjectIconStore, cache: str, out: Path) -> dict:
    renderer = Renderer(lang, store, cache, out)
    index: dict[str, int] = {}
    views: list[dict] = []
    worlds: list[DemoWorld] = []
    starts = {}

    def intern(world: DemoWorld) -> int:
        key, view = snapshot(world, renderer)
        if key not in index:
            if len(views) >= MAX_VIEWS:
                sys.exit(f"more than {MAX_VIEWS} views: bound the demo world")
            index[key] = len(views)
            views.append(view)
            worlds.append(world)
        return index[key]

    for name in SCENARIOS:
        starts[name] = intern(start_world(name, lang, store))

    done = 0
    while done < len(views):
        node, world = views[done], worlds[done]
        # The first timed change: a prompt arriving, a note expiring, a reorder.
        if world.pending_read is not None:
            nxt = fork(world, store)
            advance(nxt, 0.3)
            nxt.finish_read()
            node["to"] = [500, intern(nxt)]
        else:
            waited = 0.0
            while waited < PROBE_LIMIT_S:
                waited += TICK_S
                nxt = fork(world, store)
                advance(nxt, waited)
                target = intern(nxt)
                if target != done:
                    node["to"] = [round(waited * 1000), target]
                    break
        # A note that only fades ("Sent to ...") leaves every key where it is:
        # a press then means what it means a moment later, so it is resolved
        # there instead of capturing each view once per possible note. A
        # pending confirmation or an open usage detail is the opposite case:
        # pressing again inside the window is the whole point.
        orch = world.orch
        if (
            "to" in node
            and views[node["to"][1]]["t"] == node["t"]
            and orch._pending_confirm is None
            and not orch._usage_detail_until
        ):
            node["ff"] = node["to"][1]
        else:
            edges = {}
            for press in range(KEYS + 1):
                nxt = fork(world, store)
                advance(nxt, PRESS_GAP_S)
                nxt.run(nxt.orch.on_press(press))
                target = intern(nxt)
                if target != done:
                    edges[str(press)] = target
            node["e"] = edges
        done += 1
        if done % 100 == 0:
            print(f"  {lang}: {done}/{len(views)} views", flush=True)
    for node in views:
        hop, seen = node, set()
        while "ff" in hop and id(hop) not in seen:
            seen.add(id(hop))
            hop = views[hop["ff"]]
        node["e"] = hop.get("e", {})
        node.pop("ff", None)
    return {"version": 1, "keys": KEYS, "start": starts, "views": views}


def compact(graph: dict) -> dict:
    """Share repeated tiles and labels: most views differ in one or two keys."""
    if "tiles" in graph:
        return graph
    tiles: dict[str, int] = {}
    labels: dict[str, int] = {}

    def ref(table: dict, value) -> int:
        token = json.dumps(value)
        return table.setdefault(token, len(table))

    views = []
    for view in graph["views"]:
        slim = {
            "t": [ref(tiles, t) for t in [*view["t"], view["p"]]],
            "a": [ref(labels, a) for a in view["a"]],
            "e": view.get("e", {}),
        }
        if "to" in view:
            slim["to"] = view["to"]
        views.append(slim)
    return {
        "version": 2,
        "keys": graph["keys"],
        "start": graph["start"],
        "tiles": [json.loads(t) for t in tiles],
        "labels": [json.loads(a) for a in labels],
        "views": views,
    }


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    with TemporaryDirectory(prefix="herdeck-deck-capture-") as cache:
        for lang in LANGS:
            out = OUT / lang
            out.mkdir(parents=True)
            store = ProjectIconStore()
            graph = explore(lang, store, cache, out)
            graph = compact(graph)
            (out / "graph.json").write_text(json.dumps(graph, separators=(",", ":")))
            images = len(list(out.glob("*.webp")))
            size = sum(p.stat().st_size for p in out.iterdir()) / 1e6
            print(f"{lang}: {len(graph['views'])} views, {images} images, {size:.1f} MB")


if __name__ == "__main__":
    main()
