# Configurable Working-Agent Animation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user choose how a working agent tile animates via one global config key `[view].working_animation` ∈ `spin`/`comet`/`pulse`/`sweep`/`none`, shared across the D200, desktop window, and web simulator.

**Architecture:** The orchestrator already flags working tiles with `spinner=phase` (and idle with `None`). We add a `working_animation` string that rides on `ViewConfig` → `TileView` → `icons._compose_agent_tile`, which branches the logo/accent rendering per style **only when the tile is working** (`spinner is not None`). Idle and control tiles are untouched, and the style enters the render-cache signature only for working tiles so switching styles never churns idle-tile caches.

**Tech Stack:** Python 3.12+ (dataclasses, Pillow/PIL), pytest, ruff; Svelte 5 + Vitest for the desktop config editor.

**Spec:** `docs/superpowers/specs/2026-06-30-configurable-working-animation-design.md`

## Global Constraints

- Allowed values, exact tuple: `WORKING_ANIMATIONS = ("spin", "comet", "pulse", "sweep", "none")`.
- Default is `"spin"`; an absent `[view].working_animation` key MUST behave exactly as today (logo rotates). `TileView.working_animation` defaults to `"spin"` so direct constructions stay backward-compatible.
- An invalid value MUST raise `ConfigError` (mirror the existing unknown-tile-token validation in `settings._view_config`).
- The style affects **only** working agent tiles (`spinner is not None`). Idle/control/launcher tiles ignore it and render exactly as today.
- The style enters the `render_tile` cache signature **only when `spinner is not None`** — idle tiles keep their current cache key (no churn).
- The refactor of `_draw_spinner` MUST keep `icon_for`'s rendered output identical (same `ICON_SIZE`/`RING_INSET`/`RING_WIDTH`/math).
- Commits: Conventional Commits, English; NO `Co-Authored-By`; never squash. After each commit, check `roborev show <sha>` and fix findings.
- Before considering the branch done: `ruff check src tests` clean and `pytest` green; for the desktop task, `npm test` green in `desktop/`.

---

### Task 1: Config key — `WORKING_ANIMATIONS` + `ViewConfig.working_animation` + validation

**Files:**
- Modify: `src/herdeck/config.py` (add constant near line 65; add field to `ViewConfig` at line 76)
- Modify: `src/herdeck/settings.py` (import the constant at line 9-27; validate in `_view_config` at line 195-214)
- Test: `tests/test_settings.py`

**Interfaces:**
- Produces: `herdeck.config.WORKING_ANIMATIONS: tuple[str, ...]`; `ViewConfig.working_animation: str` (default `"spin"`). `settings._view_config(raw)` raises `ConfigError` on an invalid value and sets `view.working_animation` otherwise.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_settings.py` after `test_view_config_rejects_unknown_tile_token` (around line 522). `_view_config` and `ConfigError` are already imported in this file.

```python
def test_view_config_parses_working_animation():
    assert _view_config({"working_animation": "pulse"}).working_animation == "pulse"


def test_view_config_defaults_working_animation_to_spin():
    assert _view_config({}).working_animation == "spin"


def test_view_config_rejects_unknown_working_animation():
    with pytest.raises(ConfigError, match="unknown view.working_animation 'spinny'"):
        _view_config({"working_animation": "spinny"})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_settings.py -k working_animation -v`
Expected: FAIL — `test_view_config_parses_working_animation` and `_defaults_` fail with `AttributeError: 'ViewConfig' object has no attribute 'working_animation'`; `_rejects_` fails because no `ConfigError` is raised.

- [ ] **Step 3: Add the constant and field in `config.py`**

In `src/herdeck/config.py`, add the constant right after `TILE_LINE_TOKENS` (line 65):

```python
TILE_LINE_TOKENS: tuple[str, ...] = ("repo", "branch", "workspace", "tab", "agent")
WORKING_ANIMATIONS: tuple[str, ...] = ("spin", "comet", "pulse", "sweep", "none")
```

In the `ViewConfig` dataclass (line 76-86), add the field after `tile_secondary`:

```python
    tile_primary: list[str] | None = None
    tile_secondary: list[str] | None = None
    working_animation: str = "spin"
```

- [ ] **Step 4: Import the constant and validate in `settings.py`**

In `src/herdeck/settings.py`, add `WORKING_ANIMATIONS` to the `from .config import (...)` block (line 9-27), keeping it alphabetical near `TILE_LINE_TOKENS`:

```python
    TILE_LINE_TOKENS,
    WORKING_ANIMATIONS,
    Config,
```

In `_view_config` (line 195-214), insert this block after the `tile_primary`/`tile_secondary` loop (after line 211, before the `show_profile_on_panel` block):

```python
            setattr(view, key, tokens)
    if "working_animation" in raw:
        val = raw["working_animation"]
        if val not in WORKING_ANIMATIONS:
            raise ConfigError(
                f"unknown view.working_animation '{val}'; want one of {WORKING_ANIMATIONS}"
            )
        view.working_animation = val
    if "show_profile_on_panel" in raw:
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_settings.py -k working_animation -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/config.py src/herdeck/settings.py tests/test_settings.py
git commit -m "feat(config): add [view].working_animation key with validation"
```

---

### Task 2: Thread the style through `TileView` and the orchestrator

**Files:**
- Modify: `src/herdeck/driver/base.py` (add field to `TileView` at line 22-38)
- Modify: `src/herdeck/orchestrator.py` (agent `TileView` build at line 224-244)
- Test: `tests/test_orchestrator_tick.py`

**Interfaces:**
- Consumes: `ViewConfig.working_animation` (Task 1).
- Produces: `TileView.working_animation: str` (default `"spin"`). The orchestrator sets it from `self.config.view.working_animation` on agent tiles; control/empty tiles keep the default.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_orchestrator_tick.py`:

```python
def test_working_tile_carries_configured_animation():
    cfg = make_config()
    cfg.view.working_animation = "pulse"
    o = Orchestrator(cfg, slots=13)
    o.apply_snapshot("dev", [st("p1", Status.WORKING), st("p2", Status.IDLE)])
    o.tick()
    tiles = o.render().tiles
    assert tiles[0].working_animation == "pulse"  # working agent tile gets the config value
    assert tiles[12].working_animation == "spin"  # trailing empty tile keeps the default
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_orchestrator_tick.py::test_working_tile_carries_configured_animation -v`
Expected: FAIL with `TypeError: TileView.__init__() got an unexpected keyword argument 'working_animation'` (orchestrator passes it before the field exists) — or, if you add the orchestrator line second, `AttributeError`/assertion mismatch. Either way it must fail before both edits.

- [ ] **Step 3: Add the field to `TileView`**

In `src/herdeck/driver/base.py`, add the field right after `spinner` (line 30):

```python
    spinner: int | None = None  # rotation phase for working tiles
    working_animation: str = "spin"  # how a working tile animates ([view].working_animation)
```

- [ ] **Step 4: Pass the style from the orchestrator**

In `src/herdeck/orchestrator.py`, in the agent `TileView(...)` construction (line 224-244), add the argument right after `spinner=phase,` (line 231):

```python
                        agent_type=s.agent_type,
                        spinner=phase,
                        working_animation=self.config.view.working_animation,
                        repo=layout.compose_line(s, primary_tokens),
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/test_orchestrator_tick.py -v`
Expected: PASS (all tick tests, including the new one).

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/driver/base.py src/herdeck/orchestrator.py tests/test_orchestrator_tick.py
git commit -m "feat(orchestrator): thread working_animation onto agent tiles"
```

---

### Task 3: Extract a shared, box-parameterized comet-ring helper

**Files:**
- Modify: `src/herdeck/icons.py` (`_draw_spinner` at line 345-358)
- Test: `tests/test_icons.py`

**Interfaces:**
- Produces: `IconProvider._comet_overlay(size: int, phase: int, inset: int, width: int) -> PIL.Image.Image` — a transparent `size`×`size` RGBA overlay with an anti-aliased comet ring at rotation `phase`. `_draw_spinner(img, phase)` now delegates to it with `(ICON_SIZE, phase, RING_INSET, RING_WIDTH)`, leaving `icon_for` output unchanged.
- Consumed later by: Task 4 (`comet` style composites `_comet_overlay(62, spinner, 2, 4)` around the logo).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_icons.py` (the `_provider`/`_assets_dir` helpers already exist near line 253):

```python
def test_comet_overlay_is_phase_distinct_and_sized(tmp_path):
    from PIL import Image as _Image

    p = _provider(tmp_path / "co", _assets_dir(tmp_path, "a", "claude.svg"))
    a = p._comet_overlay(62, 0, 2, 4)
    b = p._comet_overlay(62, 2, 2, 4)
    assert isinstance(a, _Image.Image) and a.size == (62, 62)
    assert a.tobytes() != b.tobytes()  # the comet head sweeps with the phase
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_icons.py::test_comet_overlay_is_phase_distinct_and_sized -v`
Expected: FAIL with `AttributeError: 'IconProvider' object has no attribute '_comet_overlay'`.

- [ ] **Step 3: Refactor `_draw_spinner` into `_comet_overlay` + delegate**

In `src/herdeck/icons.py`, replace the whole `_draw_spinner` method (line 345-358) with:

```python
    def _comet_overlay(self, size: int, phase: int, inset: int, width: int) -> Image.Image:
        """A transparent ``size``×``size`` overlay holding an anti-aliased comet
        ring — a bright head with a fading tail — at rotation ``phase``, drawn
        supersampled then downscaled. ``inset`` and ``width`` are in final
        (pre-supersample) pixels. Shared by the full-tile spinner (``icon_for``)
        and the per-logo comet animation (``_compose_agent_tile``)."""
        z = size * _SS
        ov = Image.new("RGBA", (z, z), (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        inset_s, w = inset * _SS, width * _SS
        box = [inset_s, inset_s, z - inset_s, z - inset_s]
        head = phase * (360 / SPINNER_FRAMES)
        step = 4
        for i in range(0, RING_SPAN, step):
            alpha = int(235 * (1 - i / RING_SPAN))
            d.arc(box, head - i - step, head - i, fill=(255, 255, 255, alpha), width=w)
        return ov.resize((size, size), Image.LANCZOS)

    def _draw_spinner(self, img: Image.Image, phase: int) -> None:
        """Composite the full-tile comet ring used by ``icon_for``."""
        img.alpha_composite(self._comet_overlay(ICON_SIZE, phase, RING_INSET, RING_WIDTH))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_icons.py -k "comet_overlay or spinner_cache" -v`
Expected: PASS — the new `_comet_overlay` test and the existing `test_spinner_cache_is_bounded_to_frame_set` (icon_for path) both pass, proving the delegation preserved icon_for behavior.

- [ ] **Step 5: Commit**

```bash
git add src/herdeck/icons.py tests/test_icons.py
git commit -m "refactor(icons): extract box-parameterized comet-ring helper"
```

---

### Task 4: Render the 5 styles in `_compose_agent_tile` + cache signature

**Files:**
- Modify: `src/herdeck/icons.py` (`import math` in the import block at line 3-6; `render_tile` sig at line 370-384; `_compose_agent_tile` at line 435-487)
- Test: `tests/test_icons.py`

**Interfaces:**
- Consumes: `IconProvider._comet_overlay` (Task 3); `tile.working_animation` (Task 2). Reads it defensively with `getattr(tile, "working_animation", "spin")` so SimpleNamespace test tiles without the attribute default to `"spin"`.
- Produces: `_compose_agent_tile(tile, spinner)` branches the logo/accent render per style when `spinner is not None`; `render_tile` appends the style to the signature only when `spinner is not None`.

**Resolved ambiguity (from spec self-review):** the spec line "each of the 5 styles renders differently from the static icon" does not apply to `none` — `none` is *defined* as the static render (no animation). The binding behavior: `spin`/`comet`/`pulse`/`sweep` differ from the static idle render at a visible phase; `none` equals it; all 5 are mutually distinct at a visible phase. The tests below encode exactly that.

- [ ] **Step 1: Write the failing tests**

In `tests/test_icons.py`, change the PIL import at the top of the file (line 3) from `from PIL import Image` to:

```python
from PIL import Image, ImageDraw
```

Then add these helpers and tests at the end of the file:

```python
def _asym_rasterize(svg, size):
    # Left half white, right half transparent — so a rotation or a rescale
    # visibly changes the pixels (a uniform square would not under a 90° turn).
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle([0, 0, size // 2, size], fill=(255, 255, 255, 255))
    return img


def _anim_provider(cache_dir, assets_dir):
    return IconProvider(
        cache_dir=str(cache_dir),
        slug_map={"claude": None},
        fetch=lambda s: None,
        rasterize=_asym_rasterize,
        assets_dir=str(assets_dir),
    )


def _agent_tile(**over):
    from types import SimpleNamespace

    base = dict(
        color="green", label="", subtext=None, agent_type="claude", spinner=1,
        repo="api", branch="main", status_text="WORKING", time_text="1m",
        server_tag=None, server_accent=None, working_animation="spin",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_each_working_animation_renders_distinctly(tmp_path):
    p = _anim_provider(tmp_path / "c", _assets_dir(tmp_path, "a", "claude.svg"))
    styles = ("spin", "comet", "pulse", "sweep", "none")
    out = {s: p.render_tile_bytes(_agent_tile(working_animation=s)) for s in styles}
    assert len(set(out.values())) == 5  # all five working styles are mutually distinct


def test_none_working_matches_static_idle_and_differs_from_spin(tmp_path):
    p = _anim_provider(tmp_path / "c", _assets_dir(tmp_path, "a", "claude.svg"))
    none_working = p.render_tile_bytes(_agent_tile(working_animation="none", spinner=1))
    idle_static = p.render_tile_bytes(_agent_tile(working_animation="none", spinner=None))
    assert none_working == idle_static  # "none" disables animation -> renders like idle
    spin = p.render_tile_bytes(_agent_tile(working_animation="spin", spinner=1))
    assert none_working != spin


def test_idle_tile_renders_identically_across_styles(tmp_path):
    p = _anim_provider(tmp_path / "c", _assets_dir(tmp_path, "a", "claude.svg"))
    a = p.render_tile_bytes(_agent_tile(working_animation="spin", spinner=None))
    b = p.render_tile_bytes(_agent_tile(working_animation="sweep", spinner=None))
    assert a == b  # idle tiles ignore the style entirely


def test_working_tile_cache_key_includes_animation(tmp_path):
    p = _anim_provider(tmp_path / "c", _assets_dir(tmp_path, "a", "claude.svg"))
    spin = p.render_tile(_agent_tile(working_animation="spin", spinner=1))
    pulse = p.render_tile(_agent_tile(working_animation="pulse", spinner=1))
    assert spin != pulse  # style is part of the working-tile cache key


def test_idle_tile_cache_key_ignores_animation(tmp_path):
    p = _anim_provider(tmp_path / "c", _assets_dir(tmp_path, "a", "claude.svg"))
    a = p.render_tile(_agent_tile(working_animation="spin", spinner=None))
    b = p.render_tile(_agent_tile(working_animation="pulse", spinner=None))
    assert a == b  # idle tiles share one cache key regardless of style (no churn)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_icons.py -k "working_animation or none_working or idle_tile" -v`
Expected: FAIL — the styles all render the same (only `spin` is implemented), so `test_each_working_animation_renders_distinctly` finds < 5 distinct outputs and `test_working_tile_cache_key_includes_animation` finds equal keys.

- [ ] **Step 3: Add the `math` import**

In `src/herdeck/icons.py`, add `import math` between `import hashlib` and `import os` (line 3-4):

```python
import hashlib
import math
import os
import re
```

- [ ] **Step 4: Add the style to the render-cache signature (working tiles only)**

In `render_tile` (line 370-384), insert the working-tile style block between the `tile.time_text` element and the `server_tag` block:

```python
            tile.status_text,
            tile.time_text,
        ]
        if spinner is not None:
            sig_parts.append(getattr(tile, "working_animation", "spin"))
        if tile.server_tag or tile.server_accent:
            sig_parts.extend([tile.server_tag, tile.server_accent])
```

(Idle tiles have `spinner is None`, so their signature is byte-for-byte what it is today — no cache churn.)

- [ ] **Step 5: Replace `_compose_agent_tile` with the per-style version**

Replace the whole `_compose_agent_tile` method (line 435-487) with:

```python
    def _compose_agent_tile(self, tile, spinner=None) -> Image.Image:
        accent = COLORS.get(tile.color, COLORS["dim"])
        bg = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), TILE_BG + (255,))
        d = ImageDraw.Draw(bg)
        anim = getattr(tile, "working_animation", "spin")
        working = spinner is not None
        # logo top-left; while working it animates per the chosen style
        base_logo = self._base_glyph(tile.agent_type or "default")
        if working and anim == "pulse":
            # "breathe": scale the mark between ~0.82x and 1.0x by the spinner phase
            f = 0.82 + 0.18 * (0.5 + 0.5 * math.sin(2 * math.pi * spinner / SPINNER_FRAMES))
            s = max(1, round(46 * f))
            logo = base_logo.resize((s, s), Image.LANCZOS)
            off = 12 + (46 - s) // 2  # keep the smaller mark centred in its 46px box
            bg.alpha_composite(logo, (off, off))
        else:
            logo = base_logo.resize((46, 46), Image.LANCZOS)
            if working and anim == "spin":
                logo = logo.rotate(-spinner * SPIN_DEG, resample=Image.BICUBIC)
            bg.alpha_composite(logo, (12, 12))
            if working and anim == "comet":
                # thin comet ring orbiting the static mark; the 62px overlay is
                # centred over the 46px logo box at (12,12) -> composite at (4,4)
                bg.alpha_composite(self._comet_overlay(62, spinner, 2, 4), (4, 4))
        # status word + elapsed time, top-right
        if tile.status_text:
            fs = _font(16)
            d.text(
                (ICON_SIZE - 12 - d.textlength(tile.status_text, font=fs), 13),
                tile.status_text,
                font=fs,
                fill=accent,
            )
        if tile.time_text:
            ft = _font(15)
            d.text(
                (ICON_SIZE - 12 - d.textlength(tile.time_text, font=ft), 35),
                tile.time_text,
                font=ft,
                fill=(165, 165, 170),
            )
        # repo (primary) + branch (secondary, wrapped)
        fr = _font(23)
        d.text(
            (12, 68),
            _truncate(d, tile.repo or "", fr, ICON_SIZE - 24),
            font=fr,
            fill=(255, 255, 255),
        )
        if tile.branch:
            fb = _font(16)
            y = 98
            for line in _wrap(d, tile.branch, fb, ICON_SIZE - 24, 2):
                d.text((12, y), line, font=fb, fill=(180, 180, 188))
                y += 20
        if tile.server_tag:
            chip_fill = _rgb_color(tile.server_accent or "", (95, 95, 105))
            fc = _font(14)
            tag = _truncate(d, tile.server_tag, fc, 48)
            text_w = d.textlength(tag, font=fc)
            bb = d.textbbox((0, 0), tag, font=fc)
            x, y, pad_x, chip_h = 12, ICON_SIZE - 40, 6, 22
            chip_w = int(text_w + pad_x * 2)
            d.rounded_rectangle([x, y, x + chip_w, y + chip_h], radius=4, fill=chip_fill)
            text_y = y + (chip_h - (bb[3] - bb[1])) / 2 - bb[1]
            d.text((x + pad_x, text_y), tag, font=fc, fill=(255, 255, 255))
        # accent bar — "sweep" slides a bright segment along a dimmed bar
        y0 = ICON_SIZE - 8
        if working and anim == "sweep":
            dim = tuple(int(c * 0.4) for c in accent)
            d.rectangle([0, y0, ICON_SIZE, ICON_SIZE], fill=dim)
            seg_w = ICON_SIZE // 4
            left = int((spinner / SPINNER_FRAMES) * ICON_SIZE)
            d.rectangle([left, y0, min(left + seg_w, ICON_SIZE), ICON_SIZE], fill=accent)
            if left + seg_w > ICON_SIZE:  # wrap the bright segment past the right edge
                d.rectangle([0, y0, (left + seg_w) - ICON_SIZE, ICON_SIZE], fill=accent)
        else:
            d.rectangle([0, y0, ICON_SIZE, ICON_SIZE], fill=accent)
        return bg
```

- [ ] **Step 6: Run the new tests to verify they pass**

Run: `pytest tests/test_icons.py -k "working_animation or none_working or idle_tile" -v`
Expected: PASS (all 5 new render/cache tests).

- [ ] **Step 7: Run the full icons suite for regressions**

Run: `pytest tests/test_icons.py -v`
Expected: PASS — no existing icon test regresses (idle/control tiles and `icon_for` unchanged).

- [ ] **Step 8: Commit**

```bash
git add src/herdeck/icons.py tests/test_icons.py
git commit -m "feat(icons): render spin/comet/pulse/sweep/none working animations"
```

---

### Task 5: Desktop config editor — `working_animation` dropdown

**Files:**
- Modify: `desktop/src/lib/sections/ViewSection.svelte`
- Test: `desktop/src/lib/sections/sections.smoke.test.ts`

**Interfaces:**
- Consumes: the `[view].working_animation` key (Task 1) — written into the generic `view` section dict; server-side validation (Task 1) rejects invalid values on Apply.
- Produces: a `working_animation` `SelectField` in both base and overlay editor modes, offering exactly `["spin", "comet", "pulse", "sweep", "none"]`.

- [ ] **Step 1: Write the failing test**

Replace `desktop/src/lib/sections/sections.smoke.test.ts` with a version that also compile-smokes `ViewSection` (importing a `.svelte` compiles it, catching syntax/markup errors in the new dropdown):

```typescript
import { describe, it, expect } from "vitest";
import DesktopSection from "./DesktopSection.svelte";
import ViewSection from "./ViewSection.svelte";

// Compile-smoke only: importing a .svelte compiles it (catches syntax/compile
// errors) without a render harness.
describe("section compile-smoke", () => {
  it("compiles DesktopSection", () => {
    expect(DesktopSection).toBeTruthy();
  });

  it("compiles ViewSection", () => {
    expect(ViewSection).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run the test to verify it passes-for-the-wrong-reason check**

Run (from `desktop/`): `npm test -- sections.smoke`
Expected: PASS already (ViewSection compiles today). This smoke test guards Step 3's edit — re-run it in Step 4 to confirm the new markup still compiles. (Document this in the commit; it is the realistic frontend guard given the repo has no DOM render harness for sections — the behavioral guarantee is the server-side `ConfigError` test in Task 1.)

- [ ] **Step 3: Add the dropdown to `ViewSection.svelte`**

In `desktop/src/lib/sections/ViewSection.svelte`:

(a) Add the options constant after `MANAGEMENT` (line 17):

```javascript
  const MANAGEMENT = ["launcher_menu", "bottom_row"];
  const WORKING_ANIMATIONS = ["spin", "comet", "pulse", "sweep", "none"];
```

(b) Add the default to `VIEW_DEFAULTS` (line 23):

```javascript
  const VIEW_DEFAULTS: Record<string, unknown> = { management: "launcher_menu", agent_slots: "max", show_profile_on_panel: false, working_animation: "spin" };
```

(c) Add a base-mode derived value next to the other base derives (after `showProfile`, line 28):

```javascript
  const showProfile = $derived((getAt(payload, "base", SEC, "show_profile_on_panel") as boolean) ?? false);
  const workingAnimation = $derived((getAt(payload, "base", SEC, "working_animation") as string) ?? "spin");
```

(d) In the overlay block, add an `OverrideField` after the `show_profile_on_panel` one (line 58, before the `{#each LIST_KEYS ...}`):

```svelte
  <OverrideField label="working_animation" state={scState("working_animation")} inheritedDisplay={hint("working_animation")} onstate={(s) => setScState("working_animation", s)}>
    <SelectField label="" value={String(scValue("working_animation") ?? "spin")} options={WORKING_ANIMATIONS} onchange={(v) => setSc("working_animation", v)} />
  </OverrideField>
```

(e) In the base block, add a `SelectField` after the `show_profile_on_panel` `BooleanField` (line 65, before the `{#each LIST_KEYS ...}`):

```svelte
  <SelectField label="working_animation" value={workingAnimation} options={WORKING_ANIMATIONS} onchange={(v) => set("working_animation", v)} />
```

- [ ] **Step 4: Run the smoke test to verify the new markup compiles**

Run (from `desktop/`): `npm test -- sections.smoke`
Expected: PASS (both DesktopSection and ViewSection compile with the new dropdown).

- [ ] **Step 5: Commit**

```bash
git add desktop/src/lib/sections/ViewSection.svelte desktop/src/lib/sections/sections.smoke.test.ts
git commit -m "feat(desktop): add working_animation dropdown to View editor"
```

---

## Final verification (run before finishing the branch)

- [ ] `ruff check src tests` → clean
- [ ] `pytest` → green (whole suite)
- [ ] `cd desktop && npm test` → green
- [ ] Manual gate (macbench D200, post-merge deploy): set `[view].working_animation` to each of `spin`/`comet`/`pulse`/`sweep`/`none`, restart the deck, confirm working agents animate with the chosen style and `none` is static; idle tiles unchanged.

## Self-Review (completed by plan author)

**Spec coverage:**
- `[view].working_animation` key + `WORKING_ANIMATIONS` + validation → Task 1.
- `TileView.working_animation` threading from `ViewConfig` via orchestrator → Task 2.
- Shared box-parameterized comet helper (refactor of `_draw_spinner`, icon_for preserved) → Task 3.
- 5-style render branching in `_compose_agent_tile` + cache sig only-when-working → Task 4.
- Config editor dropdown → Task 5.
- Shared across D200/window/web automatically: covered — `_compose_agent_tile` is the single agent-tile render path (no per-deck change).
- Non-goals (per-agent style, idle/blocked animation, configurable FPS, runtime toggle) → not implemented, as required.

**Placeholder scan:** none — every code step contains the full code; no TBD/TODO.

**Type/name consistency:** `WORKING_ANIMATIONS` (config.py, imported in settings.py, mirrored as a JS literal in ViewSection); `ViewConfig.working_animation` / `TileView.working_animation` / `tile.working_animation` consistent; `_comet_overlay(size, phase, inset, width)` defined in Task 3 and called as `_comet_overlay(62, spinner, 2, 4)` in Task 4; `getattr(tile, "working_animation", "spin")` used in both the sig and `_compose_agent_tile`.
