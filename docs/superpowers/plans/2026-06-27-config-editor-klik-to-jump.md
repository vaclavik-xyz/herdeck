# Config editor klik-to-jump (preview tile → config section) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In the config window's deck preview, a tile click jumps the editor to the config section that governs that tile — by having the orchestrator tag each `TileView` with a section hint, exposing it on `/state`, and switching the click handler to "jump" in the config window.

**Architecture:** Backend tags tiles where the orchestrator builds them (it alone knows each tile's mode-dependent meaning) → deckapp `/state` exposes a `{index: section}` map → frontend `deckClient` parses it → `DeckView` gains an optional `onJump` (jump mode: click switches section, never presses) → `ConfigApp` maps the section key to its sidebar label. The floating deck (no `onJump`) keeps press behavior unchanged.

**Tech Stack:** Python (orchestrator + deckapp, pytest), Svelte 5 + TypeScript (deckClient + DeckView + ConfigApp, Vitest + Vite build).

## Global Constraints

- **`TileView.section` is additive** (`str | None = None`): every existing `TileView(...)` call stays valid; rendering/press behavior is unchanged. No new dependency.
- **Floating deck unchanged:** `DeckView` keeps today's press behavior when `onJump` is NOT passed (App.svelte). In jump mode (config preview), clicks NEVER POST `/press` and the keyboard is inert — the config preview must not actuate the deck.
- **Section-key contract** (backend → frontend, stable strings): `"view"`, `"start_profiles"`, `"answer_profiles"`, `"profiles"`, or absent/`None` (no jump). Frontend maps key → sidebar label.
- **Tile → section mapping** (verbatim, per `orchestrator.render()` modes):
  - Overview: management `profiles` action → `profiles`; management `new_agent` action → `start_profiles`; `"+ New"` launcher tile → `start_profiles`; running-agent tile → `view`; empty/dim → `None`.
  - Profile menu: profile-name tile → `profiles`; `"Back"` → `None`.
  - Launcher menu: start-profile type (has `agent_type`) → `start_profiles`; `"Profiles"` entry → `profiles`; `"Back"` → `None`.
  - Drill: answer-option tile → `answer_profiles`; `"Stop"` → `answer_profiles`; `"Back"` → `None`.
  - Panel → `None` (v1).
- Test runners: backend = `.venv/bin/python -m pytest` (pythonpath=["src"]); ruff = `.venv/bin/ruff check src tests` (BOTH dirs). Frontend = `cd desktop && npx vitest run` / `npm run build` (NO svelte-check).
- Code/comments English; UI strings Czech. No `Co-Authored-By`.

**Spec:** `docs/superpowers/specs/2026-06-27-config-editor-klik-to-jump-design.md`

---

### Task 1: TileView.section field + orchestrator tile tagging

**Files:**
- Modify: `src/herdeck/driver/base.py` (add `TileView.section`)
- Modify: `src/herdeck/orchestrator.py` (populate `section=` per mode)
- Test: `tests/test_orchestrator_sections.py` (create)

**Interfaces:**
- Produces: `TileView.section: str | None` populated by `Orchestrator.render()` per the mapping table.
- Consumes: existing `TileView`, `Orchestrator`, `_management_indices`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_orchestrator_sections.py`:

```python
from herdeck.config import AnswerProfile, Config, ConfigMeta, ServerConfig
from herdeck.model import AgentKey, AgentState, Status
from herdeck.orchestrator import Orchestrator


def _config(management="launcher_menu", profile_names=("default",)):
    cfg = Config(
        servers=[ServerConfig("dev", "wss://x", "t")],
        profiles={"default": AnswerProfile(["enter"], ["esc"], ["ctrl+c"], ["enter"])},
        overview_order=["dev"],
        grid=(5, 3),
        meta=ConfigMeta(profile_names=list(profile_names)),
    )
    cfg.view.management = management
    return cfg


def _agent(pane, status=Status.IDLE):
    return AgentState(AgentKey("dev", pane), "claude", "api", status)


def test_tileview_section_defaults_none():
    from herdeck.driver.base import TileView
    assert TileView(0, "x", "grey").section is None


def test_overview_agent_tile_section_is_view():
    o = Orchestrator(_config(), slots=13)
    o.apply_snapshot("dev", [_agent("p1")])
    assert o.render().tiles[0].section == "view"


def test_overview_launcher_tile_section_is_start_profiles():
    o = Orchestrator(_config(), slots=13)  # launcher_menu → tile slots-1 is "+ New"
    assert o.render().tiles[12].section == "start_profiles"


def test_overview_empty_tile_has_no_section():
    o = Orchestrator(_config(), slots=13)
    assert o.render().tiles[0].section is None


def test_launcher_menu_type_tiles_section_is_start_profiles():
    o = Orchestrator(_config(), slots=13)
    o.press(12)  # enter launcher via "+ New"
    assert o.render().tiles[0].section == "start_profiles"


def test_profile_menu_tiles_section_is_profiles():
    o = Orchestrator(_config(profile_names=("default", "work")), slots=13)
    o.press(12)  # enter launcher; entries = start types + "Profiles" at index 5
    o.press(5)   # press "Profiles" → profile menu
    assert o.render().tiles[0].section == "profiles"


def test_drill_tiles_section_is_answer_profiles():
    o = Orchestrator(_config(), slots=13)
    o.apply_snapshot("dev", [_agent("p1", Status.BLOCKED)])
    o.press(0)  # press the agent tile → drill
    sections = {t.section for t in o.render().tiles if t.section}
    assert sections == {"answer_profiles"}


def test_management_bottom_row_tiles_tagged():
    o = Orchestrator(_config(management="bottom_row"), slots=13)
    o.apply_snapshot("dev", [_agent("p1")])
    tagged = {t.section for t in o.render().tiles if t.section in ("profiles", "start_profiles")}
    assert tagged  # management controls carry profiles/start_profiles
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_orchestrator_sections.py -q`
Expected: FAIL — `TileView` has no `section`; `.section` is `AttributeError` / tiles untagged.

- [ ] **Step 3: Add the `TileView.section` field**

In `src/herdeck/driver/base.py`, add to the `TileView` dataclass (after the last defaulted field, e.g. after `server_accent`):

```python
    section: str | None = None  # config section a click jumps to (klik-to-jump); None = no jump
```

- [ ] **Step 4: Populate `section` in the orchestrator**

In `src/herdeck/orchestrator.py`, add a module-level constant near the other module constants (e.g. after `_MANAGEMENT_ACTIONS`):

```python
# Config section a management action's tile jumps to (klik-to-jump).
_MGMT_SECTION = {"profiles": "profiles", "new_agent": "start_profiles"}
```

Then add `section=` to the relevant `TileView(...)` constructions (leave `"Back"`/empty/dim tiles untouched — they default to `None`):

- `_render_overview`, management tile:
```python
                tiles.append(TileView(i, self._management_label(management[i]), "grey", section=_MGMT_SECTION.get(management[i])))
```
- `_render_overview`, `"+ New"` tile:
```python
                tiles.append(TileView(i, "+ New", "green", section="start_profiles"))
```
- `_render_overview`, agent tile — add `section="view"` to the `TileView(` kwargs (alongside `server_accent=accent`):
```python
                        server_accent=accent,
                        section="view",
```
- `_render_profile_menu`, profile-name tile:
```python
                tiles.append(TileView(i, label[:_OPTION_LABEL_MAX], "blue", section="profiles"))
```
- `_render_launcher`, entry tile:
```python
                tiles.append(TileView(i, entry, "blue", agent_type=agent_type, section=("start_profiles" if agent_type else "profiles")))
```
- `_render_drill`, answer-option tile:
```python
                tiles.append(TileView(i, actions[i]["label"], "blue", subtext=actions[i].get("subtext"), section="answer_profiles"))
```
- `_render_drill`, `"Stop"` tile:
```python
                tiles.append(TileView(i, "Stop", "red", section="answer_profiles"))
```

- [ ] **Step 5: Run tests to verify they pass + no regression**

Run: `.venv/bin/python -m pytest tests/test_orchestrator_sections.py tests/test_orchestrator.py tests/test_orchestrator_nav.py -q`
Expected: PASS (new + existing orchestrator tests; the additive kwarg breaks nothing).

- [ ] **Step 6: Lint + commit**

```bash
.venv/bin/ruff check src tests
git add src/herdeck/driver/base.py src/herdeck/orchestrator.py tests/test_orchestrator_sections.py
git commit -m "feat: TileView.section + orchestrator tags tiles with config sections (klik-to-jump Task 1)"
```

---

### Task 2: deckapp /state exposes tile_sections

**Files:**
- Modify: `src/herdeck/deckapp/server.py` (`_refresh_locked` captures sections; `_state` exposes them)
- Test: `tests/test_deckapp.py` (add tests)

**Interfaces:**
- Consumes: Task 1's `TileView.section` (the orchestrator's `RenderState.tiles` now carry it).
- Produces: `/state` JSON includes `"tile_sections": {index: section}` (only non-None, in-range tiles).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_deckapp.py` (uses the existing `make_app()` helper = `DeckApp(MockSource(), serve=False, icon_provider=StubIcons())`):

```python
def test_state_exposes_tile_sections():
    app = make_app()
    state = app._state()
    assert "tile_sections" in state
    sections = state["tile_sections"]
    assert isinstance(sections, dict)
    # the "+ New" launcher tile (index slots-1 = 12) is deterministically tagged
    assert sections[12] == "start_profiles"
    # only the documented section keys ever appear; empty/None tiles are omitted
    assert all(v in {"view", "start_profiles", "answer_profiles", "profiles"} for v in sections.values())
    assert all(isinstance(k, int) for k in sections)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_deckapp.py::test_state_exposes_tile_sections -q`
Expected: FAIL — `KeyError: 'tile_sections'`.

- [ ] **Step 3: Capture sections in `_refresh_locked` + expose in `_state`**

In `src/herdeck/deckapp/server.py`, in `_refresh_locked`, after the tile-PNG loop builds `new` (just before/after `self._tiles = new`), capture the section map from the same `rs.tiles`:

```python
        self._tile_sections = {
            tile.index: tile.section
            for tile in rs.tiles
            if tile.index < self._slots and tile.section
        }
```

Initialize `self._tile_sections: dict[int, str] = {}` in `__init__` (near `self._tiles`/`self._tile_ver` init) so a pre-first-refresh `_state()` is safe.

Then in `_state`, add the key:

```python
                "tiles": dict(self._tile_ver),
                "tile_sections": dict(self._tile_sections),
```

- [ ] **Step 4: Run tests to verify they pass + no regression**

Run: `.venv/bin/python -m pytest tests/test_deckapp.py -q`
Expected: PASS (new test + all existing deckapp tests; `_state` only gained a key).

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check src tests
git add src/herdeck/deckapp/server.py tests/test_deckapp.py
git commit -m "feat: deckapp /state exposes tile_sections for klik-to-jump (klik-to-jump Task 2)"
```

---

### Task 3: deckClient parses tile sections into the view model

**Files:**
- Modify: `desktop/src/lib/deckClient.ts`
- Test: `desktop/src/lib/deckClient.test.ts` (if absent, create; otherwise extend)

**Interfaces:**
- Produces: `DeckState.sections: Record<number, string>`; `DeckViewModel.sections: Record<number, string>`; `parseState` reads `tile_sections`; `stepDeck` folds `state.sections` into the view model; `initialView` seeds `{}`.
- Consumes: Task 2's `/state.tile_sections`.

- [ ] **Step 1: Write the failing tests**

Add to `desktop/src/lib/deckClient.test.ts` (import `parseState`, `stepDeck`, `initialView`, `DeckDiffer` as the file already does; if the file does not exist, create it with these imports from `./deckClient`):

```ts
import { describe, it, expect } from "vitest";
import { parseState, initialView } from "./deckClient";

describe("tile_sections parsing", () => {
  it("parseState normalizes tile_sections (string keys → number; drops junk)", () => {
    const s = parseState({ version: 1, slots: 13, tiles: {}, tile_sections: { "0": "view", "12": "start_profiles", "x": "view", "3": 5 } });
    expect(s?.sections).toEqual({ 0: "view", 12: "start_profiles" });
  });
  it("parseState yields empty sections when tile_sections is absent", () => {
    const s = parseState({ version: 1, slots: 13, tiles: {} });
    expect(s?.sections).toEqual({});
  });
  it("initialView has empty sections", () => {
    expect(initialView().sections).toEqual({});
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd desktop && npx vitest run src/lib/deckClient.test.ts`
Expected: FAIL — `s.sections` is undefined / `initialView().sections` undefined.

- [ ] **Step 3: Implement**

In `desktop/src/lib/deckClient.ts`:

Add a `sections` field to `DeckState` (after `tiles`):
```ts
  tiles: Record<number, number>; // tile index -> image version
  sections: Record<number, string>; // tile index -> config section key (klik-to-jump)
```

Add a parser (next to `parseTiles`):
```ts
/** Normalize the JSON `tile_sections` object (string keys, string values) into a
 *  numeric-keyed map, dropping non-integer indices or non-string section values. */
function parseSections(raw: unknown): Record<number, string> {
  const out: Record<number, string> = {};
  if (raw == null || typeof raw !== "object") return out;
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    const i = Number(k);
    if (Number.isInteger(i) && i >= 0 && typeof v === "string" && v) out[i] = v;
  }
  return out;
}
```

In `parseState`, set `sections: parseSections(v.tile_sections),` (next to `tiles: parseTiles(v.tiles),`).

Add `sections` to `DeckViewModel` (after `tiles`):
```ts
  tiles: Record<number, string>; // index -> img src
  sections: Record<number, string>; // index -> config section key (klik-to-jump)
```

In `initialView`, add `sections: {},` to the returned object.

In `stepDeck`, add `sections: state.sections,` to the returned view-model object (the section map is cheap and always reflects the latest `/state`, so it is set directly, not diffed).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd desktop && npx vitest run src/lib/deckClient.test.ts && npx vitest run`
Expected: PASS (new + all existing).

- [ ] **Step 5: Commit**

```bash
git add desktop/src/lib/deckClient.ts desktop/src/lib/deckClient.test.ts
git commit -m "feat: deckClient parses tile_sections into the view model (klik-to-jump Task 3)"
```

---

### Task 4: DeckView jump mode (onJump)

**Files:**
- Modify: `desktop/src/lib/DeckView.svelte`

**Interfaces:**
- Consumes: Task 3's `view.sections`. Adds optional prop `onJump?: (section: string) => void`.
- Behavior: when `onJump` is provided = jump mode — a tile click calls `onJump(view.sections[i])` (only when a section exists) and NEVER presses; keyboard is disabled. When `onJump` is absent = today's press behavior (unchanged).

- [ ] **Step 1: Add the prop + branch the click/keyboard**

In `DeckView.svelte` `<script>`, extend props:

```ts
  let {
    transport,
    pollMs = 300,
    onJump = undefined,
  }: {
    transport: DeckTransport | null;
    pollMs?: number;
    onJump?: (section: string) => void;
  } = $props();
```

Add a click handler that branches on mode (place near `press`):

```ts
  // Config-window preview passes onJump → "jump mode": a tile click switches the editor
  // to that tile's config section and NEVER actuates the deck. The floating deck leaves
  // onJump undefined and keeps the press behavior below.
  function clickTile(i: number): void {
    if (onJump) {
      const section = view.sections[i];
      if (section) onJump(section);
      return;
    }
    void press(i);
  }
```

Change the tile button's handler from `onclick={() => void press(i)}` to `onclick={() => clickTile(i)}`. Leave the panel button as `onclick={() => void press(view.slots)}` BUT guard it for jump mode (the panel has no section): wrap it `onclick={() => { if (!onJump) void press(view.slots); }}`.

In `onKey`, make the keyboard inert in jump mode — add as the first line of `onKey`:

```ts
    if (onJump) return; // jump-mode preview never actuates via keyboard
```

(Optionally add `class:jump={onJump}` / `title` affordance — not required; keep scope tight.)

- [ ] **Step 2: Verify build + smoke**

Run: `cd desktop && npm run build && npx vitest run src/lib/fields/widgets.smoke.test.ts`
Expected: build exit 0; smoke PASS. (DeckView has no render harness; build is the gate — same as other components.)

- [ ] **Step 3: Commit**

```bash
git add desktop/src/lib/DeckView.svelte
git commit -m "feat: DeckView jump mode (onJump) — preview click jumps instead of pressing (klik-to-jump Task 4)"
```

---

### Task 5: ConfigApp wires the preview to jump to sections

**Files:**
- Modify: `desktop/src/ConfigApp.svelte`

**Interfaces:**
- Consumes: Task 4's `DeckView.onJump`. Maps the backend section KEY → the sidebar section LABEL and sets `active`.

- [ ] **Step 1: Add the key→label map + pass onJump**

In `ConfigApp.svelte` `<script>`, add the mapping near the `SECTIONS` constant:

```ts
  // Backend tile section KEY (deckClient) → this editor's sidebar section LABEL.
  const SECTION_FOR_KEY: Record<string, string> = {
    view: "View",
    start_profiles: "Start profiles",
    answer_profiles: "Answer profiles",
    profiles: "Profiles",
  };
  function jumpToSection(key: string): void {
    const label = SECTION_FOR_KEY[key];
    if (label) active = label;
  }
```

In the preview `<aside class="preview">`, pass `onJump` to the `DeckView`:

```svelte
    <aside class="preview">
      <DeckView transport={preview} onJump={jumpToSection} />
    </aside>
```

- [ ] **Step 2: Verify build + full suite**

Run: `cd desktop && npm run build && npx vitest run`
Expected: build exit 0; all Vitest pass (no regression).

- [ ] **Step 3: Commit**

```bash
git add desktop/src/ConfigApp.svelte
git commit -m "feat: ConfigApp preview tile click jumps to its config section (klik-to-jump Task 5)"
```

---

## Self-Review

**Spec coverage:** TileView.section + orchestrator tagging (Task 1) · deckapp /state exposure (Task 2) · deckClient parse (Task 3) · DeckView jump mode (Task 4) · ConfigApp wiring + key→label map (Task 5). The full mapping table, the click-conflict resolution (jump mode never presses), and the agent-tile→view decision are all covered. Non-goals (panel jump, field-level jump, static preview, modifier-click) correctly excluded.

**Type consistency:** `section: str | None` (Py) ↔ `tile_sections: {index: section}` (`/state`) ↔ `sections: Record<number,string>` (deckClient `DeckState`/`DeckViewModel`) ↔ `onJump: (section: string) => void` (DeckView) ↔ `SECTION_FOR_KEY[key] → label` (ConfigApp). Section keys are the single stable contract `view`/`start_profiles`/`answer_profiles`/`profiles`. Floating deck (`App.svelte`, no `onJump`) is untouched → press behavior preserved.

**No placeholders:** every step carries concrete code, the exact run command, and expected output.
