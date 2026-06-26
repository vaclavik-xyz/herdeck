# Config editor frontend řez 4b-ii-β1 (overlay mechanism + Tier-1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Tier-1 sections (View / Deck / Safety / Theme) editable as profile OVERLAYS when a named profile is active — per-field inherit/override via chain-aware inherited values — while base mode stays exactly as today.

**Architecture:** A pure overlay-resolution layer in `configClient` (chain-aware `inheritedFor`, `overrideState`, override readers + n-level path mutators for `theme.colors`), a scalar `OverrideField` widget + an overlay mode on `TriStateListField`, and an `editProfile` prop that switches each Tier-1 section between base rendering (unchanged) and overlay rendering. The active-profile switcher (řez 4b-i) doubles as the edit target; ConfigApp passes `editProfile` and notes that non-Tier-1 sections still edit base.

**Tech Stack:** Svelte 5 (runes + snippets), TypeScript, Vitest. Desktop app under `desktop/`.

**Spec:** `docs/superpowers/specs/2026-06-26-config-editor-frontend-4b-ii-beta1-design.md`

## Global Constraints

- **Overlay semantics mirror backend `_merge_section`:** dict sections merge per-key recursively, lists/scalars replace wholesale; the inherited value resolves down the `extends` chain (base-most parent → immediate parent), EXCLUDING the profile's own overlay. `theme.colors` merges per-status, so a per-status color override lives at `profiles[X].theme.colors.<status>` (2 levels under the section).
- **Overlay tri-state for lists reuses α's `ListFieldState`:** in overlay context `"default"` MEANS "inherit" (key absent in the overlay → inherited value); `"empty"` = explicit `[]`; `"custom"` = a list. Clearing an override removes the key (`clearOverride`).
- **Base mode is unchanged.** When `editProfile` is null/"default", every Tier-1 section renders exactly as it does today (α). No base-mode regression.
- **Hardware is never overlaid.** DeckSection's Hardware fieldset always edits `local.toml`, in base AND overlay mode.
- **Svelte 5 runes + snippets** (`$props`, `$derived`, `$bindable`, `Snippet`, `{@render}`). Component verification = **build gate** (`npm run build` exit 0) + compile-smoke; there is NO svelte-check and NO render/interaction harness in this repo (consistent with řez 4a/4b-i/α).
- **No new runtime deps, no new HTTP routes, no new Tauri commands, no backend (Python) change.** Token never in JS; secrets one-way (β1 touches no secrets).
- **Czech UI copy, English code/identifiers/commits.** Conventional commits; no `Co-Authored-By`.
- **Test runners:** desktop = `cd desktop && npx vitest run` (single file: `npx vitest run src/lib/configClient.test.ts`) / `npm run build`.

---

### Task 1: configClient overlay-resolution helpers

**Files:**
- Modify: `desktop/src/lib/configClient.ts` (add after the `setListField` block from α, near the end)
- Test: `desktop/src/lib/configClient.test.ts` (add a new `describe` block; extend the import list)

**Interfaces:**
- Consumes: existing `ConfigPayload`, `ListFieldState`, `inheritedValue` (řez-3, kept), `asDict`/`clone` (private, already in file).
- Produces:
  - `inheritedFor(payload: ConfigPayload, profile: string, section: string, key: string): unknown`
  - `inheritedForPath(payload: ConfigPayload, profile: string, path: string[]): unknown`
  - `overrideValue(payload: ConfigPayload, profile: string, section: string, key: string): unknown`
  - `overrideValuePath(payload: ConfigPayload, profile: string, path: string[]): unknown`
  - `overrideState(payload: ConfigPayload, profile: string, section: string, key: string): ListFieldState`
  - `setOverridePath(profiles: Record<string, Record<string, unknown>>, name: string, path: string[], value: unknown): Record<string, Record<string, unknown>>`
  - `clearOverridePath(profiles: Record<string, Record<string, unknown>>, name: string, path: string[]): Record<string, Record<string, unknown>>`

- [ ] **Step 1: Write the failing tests**

Add `inheritedFor`, `inheritedForPath`, `overrideValue`, `overrideValuePath`, `overrideState`, `setOverridePath`, `clearOverridePath` to the top `import { … } from "./configClient"` block, then append:

```ts
describe("overlay resolution (β1)", () => {
  // base.view.management = launcher_menu; parent overrides it; child extends parent.
  const payload = parseConfig({
    base: { view: { management: "launcher_menu", agent_slots: "2" }, theme: { colors: { blocked: "#f00" } } },
    profiles: {
      parent: { view: { management: "bottom_row" } },
      child: { extends: "parent", view: { agent_slots: "4" } },
      mob: { view: { tile_primary: [] } },
    },
  })!;

  it("inheritedFor resolves through the extends chain, excluding the profile's own overlay", () => {
    // child inherits management from parent (parent overrode base), agent_slots from base (child overrides it itself)
    expect(inheritedFor(payload, "child", "view", "management")).toBe("bottom_row");
    expect(inheritedFor(payload, "child", "view", "agent_slots")).toBe("2");
    // parent inherits management from base (its own override is excluded)
    expect(inheritedFor(payload, "parent", "view", "management")).toBe("launcher_menu");
    // absent everywhere → undefined
    expect(inheritedFor(payload, "parent", "view", "missing")).toBeUndefined();
  });

  it("inheritedFor falls back to base on an unknown/cyclic extends target", () => {
    const cyc = parseConfig({
      base: { view: { management: "launcher_menu" } },
      profiles: { a: { extends: "b", view: {} }, b: { extends: "a", view: { management: "bottom_row" } } },
    })!;
    // a extends b extends a → cycle; walk stops, falls back to base
    expect(inheritedFor(cyc, "a", "view", "management")).toBe("launcher_menu");
  });

  it("inheritedForPath resolves a nested path (theme.colors.<status>) through the chain", () => {
    expect(inheritedForPath(payload, "mob", ["theme", "colors", "blocked"])).toBe("#f00");
    expect(inheritedForPath(payload, "mob", ["theme", "colors", "idle"])).toBeUndefined();
  });

  it("overrideValue / overrideValuePath read the profile's own overlay only", () => {
    expect(overrideValue(payload, "parent", "view", "management")).toBe("bottom_row");
    expect(overrideValue(payload, "child", "view", "management")).toBeUndefined(); // child does not override it
    expect(overrideValuePath(payload, "parent", ["view", "management"])).toBe("bottom_row");
  });

  it("overrideState reports inherit / empty / custom", () => {
    expect(overrideState(payload, "child", "view", "management")).toBe("default"); // absent → inherit
    expect(overrideState(payload, "mob", "view", "tile_primary")).toBe("empty");   // []
    expect(overrideState(payload, "parent", "view", "management")).toBe("custom"); // a value
  });

  it("setOverridePath creates a nested override without touching the input", () => {
    const next = setOverridePath(payload.profiles, "mob", ["theme", "colors", "blocked"], "#0f0");
    expect((next.mob.theme as any).colors.blocked).toBe("#0f0");
    expect(payload.profiles.mob.theme).toBeUndefined(); // input untouched
  });

  it("clearOverridePath removes the leaf and prunes emptied ancestors, keeping the profile entry", () => {
    const withColor = setOverridePath(payload.profiles, "mob", ["theme", "colors", "blocked"], "#0f0");
    const cleared = clearOverridePath(withColor, "mob", ["theme", "colors", "blocked"]);
    expect(cleared.mob.theme).toBeUndefined(); // colors emptied → theme pruned
    expect("mob" in cleared).toBe(true);       // profile entry kept
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd desktop && npx vitest run src/lib/configClient.test.ts`
Expected: FAIL — the seven helpers are not exported.

- [ ] **Step 3: Implement the helpers**

In `desktop/src/lib/configClient.ts`, after the `setListField` function, add:

```ts
// --- profile overlay resolution (řez 4b-ii-β1) ---

/** Read a path under `root`; returns whether every level was present + the value. */
function readPath(root: unknown, path: string[]): { found: boolean; value: unknown } {
  let cur: unknown = root;
  for (const k of path) {
    if (cur == null || typeof cur !== "object" || Array.isArray(cur) || !(k in (cur as Record<string, unknown>))) {
      return { found: false, value: undefined };
    }
    cur = (cur as Record<string, unknown>)[k];
  }
  return { found: true, value: cur };
}

/** Overlay dicts a profile INHERITS: base-most parent down to but EXCLUDING `profile`
 *  itself (the overlays via `extends`). Mirrors backend `_profile_overlays` minus the
 *  profile's own overlay. A cycle or unknown target stops the walk (editor falls back to
 *  base; backend rejects on write). */
function inheritedChain(
  profiles: Record<string, Record<string, unknown>>,
  profile: string,
): Record<string, unknown>[] {
  const chain: string[] = [];
  const seen = new Set<string>();
  const ext0 = asDict(profiles[profile]).extends;
  let cur: string | undefined = typeof ext0 === "string" ? ext0 : undefined;
  while (cur && cur !== "default") {
    if (seen.has(cur) || !(cur in profiles)) break;
    seen.add(cur);
    chain.push(cur);
    const ext = asDict(profiles[cur]).extends;
    cur = typeof ext === "string" ? ext : undefined;
  }
  return chain.reverse().map((n) => asDict(profiles[n]));
}

/** The value `profile` inherits at `path` (base + parent overlays via extends, excluding
 *  the profile's own overlay), or undefined when absent everywhere. */
export function inheritedForPath(payload: ConfigPayload, profile: string, path: string[]): unknown {
  let value = readPath(payload.base, path).value;
  for (const overlay of inheritedChain(payload.profiles, profile)) {
    const r = readPath(overlay, path);
    if (r.found) value = r.value;
  }
  return value;
}

/** Chain-aware inherited value for `section.key` (the common 2-level case). */
export function inheritedFor(payload: ConfigPayload, profile: string, section: string, key: string): unknown {
  return inheritedForPath(payload, profile, [section, key]);
}

/** The raw value at `path` in `profile`'s OWN overlay (no inheritance), or undefined. */
export function overrideValuePath(payload: ConfigPayload, profile: string, path: string[]): unknown {
  return readPath(payload.profiles[profile], path).value;
}

/** The raw override value for `section.key` in `profile`'s overlay, or undefined. */
export function overrideValue(payload: ConfigPayload, profile: string, section: string, key: string): unknown {
  return overrideValuePath(payload, profile, [section, key]);
}

/** Override state of `section.key` in `profile`'s overlay: absent → "default" (= inherit),
 *  `[]` → "empty", anything else present → "custom". Reuses `ListFieldState`; in overlay
 *  context "default" denotes inheritance. */
export function overrideState(payload: ConfigPayload, profile: string, section: string, key: string): ListFieldState {
  const { found, value } = readPath(payload.profiles[profile], [section, key]);
  if (!found) return "default";
  return Array.isArray(value) && value.length === 0 ? "empty" : "custom";
}

/** NEW profiles map writing `profiles[name]<path> = value`, creating nested dicts as
 *  needed. Input untouched. */
export function setOverridePath(
  profiles: Record<string, Record<string, unknown>>,
  name: string,
  path: string[],
  value: unknown,
): Record<string, Record<string, unknown>> {
  const next = clone(profiles);
  let cur: Record<string, unknown> = next[name] ?? (next[name] = {});
  for (let i = 0; i < path.length - 1; i++) {
    const k = path[i];
    const child = cur[k];
    if (child == null || typeof child !== "object" || Array.isArray(child)) cur[k] = {};
    cur = cur[k] as Record<string, unknown>;
  }
  cur[path[path.length - 1]] = value;
  return next;
}

/** NEW profiles map with `profiles[name]<path>` removed; emptied ancestor dicts are pruned
 *  up to (but not including) the profile entry, which is kept. Input untouched. */
export function clearOverridePath(
  profiles: Record<string, Record<string, unknown>>,
  name: string,
  path: string[],
): Record<string, Record<string, unknown>> {
  const next = clone(profiles);
  const stack: Record<string, unknown>[] = [];
  let cur = next[name] as Record<string, unknown> | undefined;
  if (cur == null) return next;
  for (let i = 0; i < path.length - 1; i++) {
    stack.push(cur);
    const child = cur[path[i]];
    if (child == null || typeof child !== "object" || Array.isArray(child)) return next; // path absent
    cur = child as Record<string, unknown>;
  }
  stack.push(cur);
  delete cur[path[path.length - 1]];
  for (let i = stack.length - 1; i >= 1; i--) {
    if (Object.keys(stack[i]).length === 0) delete stack[i - 1][path[i - 1]];
    else break;
  }
  return next;
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd desktop && npx vitest run src/lib/configClient.test.ts`
Expected: PASS (all new + all pre-existing).

- [ ] **Step 5: Commit**

```bash
git add desktop/src/lib/configClient.ts desktop/src/lib/configClient.test.ts
git commit -m "feat: configClient overlay resolution (chain-aware inherited + path override)"
```

---

### Task 2: `OverrideField.svelte` scalar wrapper

**Files:**
- Create: `desktop/src/lib/fields/OverrideField.svelte`
- Test: `desktop/src/lib/fields/widgets.smoke.test.ts` (add compile-smoke)

**Interfaces:**
- Produces: a widget with props `{ label: string; state: "inherit" | "override"; inheritedDisplay: string; onstate: (s: "inherit" | "override") => void; children: Snippet }`. In "override" it renders the provided scalar widget (a snippet); in "inherit" it shows the dimmed inherited value. Sections render the inner widget with `label=""` (OverrideField owns the label — same nesting pattern as `TriStateListField`→`ListField`).

- [ ] **Step 1: Create the widget**

Create `desktop/src/lib/fields/OverrideField.svelte`:

```svelte
<script lang="ts">
  import type { Snippet } from "svelte";

  let { label, state, inheritedDisplay, onstate, children }:
    {
      label: string;
      state: "inherit" | "override";
      inheritedDisplay: string;
      onstate: (s: "inherit" | "override") => void;
      children: Snippet;
    } = $props();

  const SEGMENTS: { value: "inherit" | "override"; text: string }[] = [
    { value: "inherit", text: "Zdědit" },
    { value: "override", text: "Vlastní" },
  ];

  function pick(next: "inherit" | "override"): void {
    if (next !== state) onstate(next);
  }
</script>

<div class="override">
  <span class="label">{label}</span>
  <div class="body">
    <div class="seg" role="group" aria-label={label}>
      {#each SEGMENTS as s}
        <button
          type="button"
          class:on={s.value === state}
          aria-pressed={s.value === state}
          onclick={() => pick(s.value)}
        >{s.text}</button>
      {/each}
    </div>
    {#if state === "override"}
      {@render children()}
    {:else}
      <p class="hint">zděděno: {inheritedDisplay}</p>
    {/if}
  </div>
</div>

<style>
  .override { display: grid; grid-template-columns: 120px 1fr; align-items: start; gap: 8px; margin: 6px 0; }
  .label { color: #aaa; padding-top: 4px; }
  .body { display: flex; flex-direction: column; gap: 4px; }
  .seg { display: inline-flex; align-self: flex-start; border: 1px solid #2a2a30; border-radius: 4px; overflow: hidden; }
  .seg button { background: #141417; border: 0; border-right: 1px solid #2a2a30; color: #aaa; padding: 4px 10px; cursor: pointer; }
  .seg button:last-child { border-right: 0; }
  .seg button.on { background: #2a2a30; color: #e8e8ea; }
  .hint { color: #777; margin: 2px 0; font-style: italic; }
</style>
```

- [ ] **Step 2: Add the compile-smoke**

In `desktop/src/lib/fields/widgets.smoke.test.ts` add the import and a case:

```ts
import OverrideField from "./OverrideField.svelte";
```
```ts
  it("compiles OverrideField", () => {
    expect(OverrideField).toBeTruthy();
  });
```

Run: `cd desktop && npx vitest run src/lib/fields/widgets.smoke.test.ts` → PASS.

- [ ] **Step 3: Verify the build**

Run: `cd desktop && npm run build` → exit 0.

- [ ] **Step 4: Commit**

```bash
git add desktop/src/lib/fields/OverrideField.svelte desktop/src/lib/fields/widgets.smoke.test.ts
git commit -m "feat: OverrideField scalar overlay wrapper (Zdědit/Vlastní)"
```

---

### Task 3: `TriStateListField` overlay mode

**Files:**
- Modify: `desktop/src/lib/fields/TriStateListField.svelte`

**Interfaces:**
- Adds optional props `inheritLabel?: string` (relabels the first segment; default "Výchozí") and `inheritHint?: string` (the dimmed hint shown in the first state; overrides the `defaultHint` formatting when provided). Behavior otherwise identical (3-state, `[""]` seed on custom-from-empty). Used in overlay mode with `inheritLabel="Zdědit"` + `inheritHint="<inherited value>"`.

- [ ] **Step 1: Edit the widget**

In `desktop/src/lib/fields/TriStateListField.svelte`, change the props destructure
```ts
  let { label, state, list, defaultHint, onchange }:
    {
      label: string;
      state: ListFieldState;
      list: string[];
      defaultHint?: string;
      onchange: (state: ListFieldState, list: string[]) => void;
    } = $props();
```
to
```ts
  let { label, state, list, defaultHint, inheritLabel, inheritHint, onchange }:
    {
      label: string;
      state: ListFieldState;
      list: string[];
      defaultHint?: string;
      inheritLabel?: string;
      inheritHint?: string;
      onchange: (state: ListFieldState, list: string[]) => void;
    } = $props();
```
Replace the `const SEGMENTS` array
```ts
  const SEGMENTS: { value: ListFieldState; text: string }[] = [
    { value: "default", text: "Výchozí" },
    { value: "custom", text: "Vlastní" },
    { value: "empty", text: "Vypnuto" },
  ];
```
with a `$derived` that uses `inheritLabel`:
```ts
  const SEGMENTS = $derived<{ value: ListFieldState; text: string }[]>([
    { value: "default", text: inheritLabel ?? "Výchozí" },
    { value: "custom", text: "Vlastní" },
    { value: "empty", text: "Vypnuto" },
  ]);
```
Replace the default-state hint line
```svelte
    {:else if state === "default"}
      <p class="hint">{defaultHint ? `výchozí: ${defaultHint}` : "(výchozí)"}</p>
```
with
```svelte
    {:else if state === "default"}
      <p class="hint">{inheritHint ?? (defaultHint ? `výchozí: ${defaultHint}` : "(výchozí)")}</p>
```

- [ ] **Step 2: Verify the build + suite**

Run: `cd desktop && npm run build` → exit 0.
Run: `cd desktop && npx vitest run` → PASS (existing base-mode usages unaffected — new props are optional).

- [ ] **Step 3: Commit**

```bash
git add desktop/src/lib/fields/TriStateListField.svelte
git commit -m "feat: TriStateListField overlay mode (inheritLabel/inheritHint)"
```

---

### Task 4: ViewSection overlay-aware

**Files:**
- Modify: `desktop/src/lib/sections/ViewSection.svelte`

**Interfaces:**
- Consumes: `OverrideField` (Task 2), overlay-mode `TriStateListField` (Task 3), configClient `inheritedFor`/`overrideState`/`overrideValue`/`setOverride`/`clearOverride` (řez-3 `setOverride`/`clearOverride` are 2-level: `profiles[name][section][key]`).
- Adds an optional `editProfile?: string | null` prop. Null/"default" → base render (unchanged from α). A named profile → overlay render.

- [ ] **Step 1: Rewrite the section**

Replace the entire contents of `desktop/src/lib/sections/ViewSection.svelte` with:

```svelte
<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import SelectField from "../fields/SelectField.svelte";
  import BooleanField from "../fields/BooleanField.svelte";
  import TriStateListField from "../fields/TriStateListField.svelte";
  import OverrideField from "../fields/OverrideField.svelte";
  import {
    getAt, setAt, listFieldState, setListField,
    inheritedFor, overrideState, overrideValue, setOverride, clearOverride,
    type ListFieldState, type ConfigPayload,
  } from "../configClient";

  let { payload = $bindable(), onChange, editProfile = null }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; editProfile?: string | null } = $props();

  const SEC = "view";
  const MANAGEMENT = ["launcher_menu", "bottom_row"];
  const LIST_KEYS = ["bottom_row", "tile_fields", "tile_primary", "tile_secondary"] as const;
  const overlay = $derived(editProfile != null && editProfile !== "default");
  const prof = $derived(editProfile ?? "");

  // --- base mode (unchanged from α) ---
  const management = $derived((getAt(payload, "base", SEC, "management") as string) ?? "launcher_menu");
  const agentSlots = $derived((getAt(payload, "base", SEC, "agent_slots") as string) ?? "");
  const showProfile = $derived((getAt(payload, "base", SEC, "show_profile_on_panel") as boolean) ?? false);
  function set(key: string, value: unknown): void { payload = setAt(payload, "base", SEC, key, value); onChange(); }
  function setBaseTri(key: string, state: ListFieldState, list: string[]): void { payload = setListField(payload, "base", SEC, key, state, list); onChange(); }

  // --- overlay mode helpers ---
  function hint(key: string): string { const v = inheritedFor(payload, prof, SEC, key); return Array.isArray(v) ? v.join(" · ") : v == null ? "(nic)" : String(v); }
  function scState(key: string): "inherit" | "override" { return overrideState(payload, prof, SEC, key) === "default" ? "inherit" : "override"; }
  function scValue(key: string): unknown { const v = overrideValue(payload, prof, SEC, key); return v === undefined ? inheritedFor(payload, prof, SEC, key) : v; }
  function setScState(key: string, s: "inherit" | "override"): void {
    payload = { ...payload, profiles: s === "inherit" ? clearOverride(payload.profiles, prof, SEC, key) : setOverride(payload.profiles, prof, SEC, key, inheritedFor(payload, prof, SEC, key)) };
    onChange();
  }
  function setSc(key: string, v: unknown): void { payload = { ...payload, profiles: setOverride(payload.profiles, prof, SEC, key, v) }; onChange(); }
  function ovListValue(key: string): string[] { const v = overrideValue(payload, prof, SEC, key); return Array.isArray(v) ? v as string[] : []; }
  function setOvList(key: string, state: ListFieldState, list: string[]): void {
    payload = { ...payload, profiles: state === "default" ? clearOverride(payload.profiles, prof, SEC, key) : setOverride(payload.profiles, prof, SEC, key, state === "empty" ? [] : list) };
    onChange();
  }
</script>

<h2>View{#if overlay} · overlay: {editProfile}{/if}</h2>
{#if overlay}
  <OverrideField label="management" state={scState("management")} inheritedDisplay={hint("management")} onstate={(s) => setScState("management", s)}>
    <SelectField label="" value={String(scValue("management") ?? "launcher_menu")} options={MANAGEMENT} onchange={(v) => setSc("management", v)} />
  </OverrideField>
  <OverrideField label="agent_slots" state={scState("agent_slots")} inheritedDisplay={hint("agent_slots")} onstate={(s) => setScState("agent_slots", s)}>
    <TextField label="" value={String(scValue("agent_slots") ?? "")} oninput={(v) => setSc("agent_slots", v)} />
  </OverrideField>
  <OverrideField label="show_profile_on_panel" state={scState("show_profile_on_panel")} inheritedDisplay={hint("show_profile_on_panel")} onstate={(s) => setScState("show_profile_on_panel", s)}>
    <BooleanField label="" value={Boolean(scValue("show_profile_on_panel"))} onchange={(v) => setSc("show_profile_on_panel", v)} />
  </OverrideField>
  {#each LIST_KEYS as key}
    <TriStateListField label={key} state={overrideState(payload, prof, SEC, key)} list={ovListValue(key)} inheritLabel="Zdědit" inheritHint={`zděděno: ${hint(key)}`} onchange={(s, l) => setOvList(key, s, l)} />
  {/each}
{:else}
  <SelectField label="management" value={management} options={MANAGEMENT} onchange={(v) => set("management", v)} />
  <TextField label="agent_slots" value={agentSlots} oninput={(v) => set("agent_slots", v)} />
  <BooleanField label="show_profile_on_panel" value={showProfile} onchange={(v) => set("show_profile_on_panel", v)} />
  {#each LIST_KEYS as key}
    <TriStateListField label={key} state={listFieldState(payload, "base", SEC, key)} list={(getAt(payload, "base", SEC, key) as string[]) ?? []} onchange={(s, l) => setBaseTri(key, s, l)} />
  {/each}
{/if}

<style>
  h2 { margin: 0 0 8px; }
</style>
```

- [ ] **Step 2: Verify the build + suite**

Run: `cd desktop && npm run build` → exit 0.
Run: `cd desktop && npx vitest run` → PASS.

- [ ] **Step 3: Commit**

```bash
git add desktop/src/lib/sections/ViewSection.svelte
git commit -m "feat: ViewSection overlay-aware (per-field inherit/override)"
```

---

### Task 5: DeckSection overlay-aware

**Files:**
- Modify: `desktop/src/lib/sections/DeckSection.svelte`

**Interfaces:**
- Same overlay helpers as Task 4, for `SEC = "deck"`. The **Hardware fieldset stays local-only** and is rendered OUTSIDE the base/overlay branch (always edits `local.toml`). `grid` (scalar) + `overview_order` (list) are the only overlay-eligible fields.

- [ ] **Step 1: Rewrite the section**

Replace the entire contents of `desktop/src/lib/sections/DeckSection.svelte` with:

```svelte
<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import NumberField from "../fields/NumberField.svelte";
  import TriStateListField from "../fields/TriStateListField.svelte";
  import OverrideField from "../fields/OverrideField.svelte";
  import {
    getAt, setAt, removeAt, listFieldState, setListField, serversOf,
    inheritedFor, overrideState, overrideValue, setOverride, clearOverride,
    type ListFieldState, type ConfigPayload,
  } from "../configClient";

  let { payload = $bindable(), onChange, editProfile = null }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; editProfile?: string | null } = $props();

  const SEC = "deck";
  const overlay = $derived(editProfile != null && editProfile !== "default");
  const prof = $derived(editProfile ?? "");
  const serverHint = $derived(serversOf(payload).map((s) => s.id).filter((id) => id !== "").join(" · "));

  // --- base mode (grid + overview_order) ---
  const grid = $derived((getAt(payload, "base", SEC, "grid") as string) ?? "");
  const overviewState = $derived(listFieldState(payload, "base", SEC, "overview_order"));
  const overviewOrder = $derived((getAt(payload, "base", SEC, "overview_order") as string[]) ?? []);
  function setBase(key: string, value: unknown): void { payload = setAt(payload, "base", SEC, key, value); onChange(); }
  function setBaseOverview(state: ListFieldState, list: string[]): void { payload = setListField(payload, "base", SEC, "overview_order", state, list); onChange(); }

  // --- overlay helpers (grid + overview_order) ---
  function hint(key: string): string { const v = inheritedFor(payload, prof, SEC, key); return Array.isArray(v) ? v.join(" · ") : v == null ? "(nic)" : String(v); }
  function scState(key: string): "inherit" | "override" { return overrideState(payload, prof, SEC, key) === "default" ? "inherit" : "override"; }
  function scValue(key: string): unknown { const v = overrideValue(payload, prof, SEC, key); return v === undefined ? inheritedFor(payload, prof, SEC, key) : v; }
  function setScState(key: string, s: "inherit" | "override"): void {
    payload = { ...payload, profiles: s === "inherit" ? clearOverride(payload.profiles, prof, SEC, key) : setOverride(payload.profiles, prof, SEC, key, inheritedFor(payload, prof, SEC, key)) };
    onChange();
  }
  function setSc(key: string, v: unknown): void { payload = { ...payload, profiles: setOverride(payload.profiles, prof, SEC, key, v) }; onChange(); }
  function ovOverviewList(): string[] { const v = overrideValue(payload, prof, SEC, "overview_order"); return Array.isArray(v) ? v as string[] : []; }
  function setOvOverview(state: ListFieldState, list: string[]): void {
    payload = { ...payload, profiles: state === "default" ? clearOverride(payload.profiles, prof, SEC, "overview_order") : setOverride(payload.profiles, prof, SEC, "overview_order", state === "empty" ? [] : list) };
    onChange();
  }

  // Hardware (local.toml) — never overlaid, always local.
  const hwDeck = $derived((getAt(payload, "local", "local", "deck") as string) ?? "");
  const hwSocket = $derived((getAt(payload, "local", "local", "herdr_socket") as string) ?? "");
  const hwBind = $derived((getAt(payload, "local", "local", "web_bind") as string) ?? "");
  const hwIcons = $derived((getAt(payload, "local", "local", "icons_dir") as string) ?? "");
  const hwPort = $derived((getAt(payload, "local", "local", "web_port") as number | null) ?? null);
  const brightness = $derived((getAt(payload, "local", "hardware", "brightness") as number | null) ?? null);
  const debounce = $derived((getAt(payload, "local", "hardware", "debounce") as number | null) ?? null);
  const keepAlive = $derived((getAt(payload, "local", "hardware", "keep_alive_interval") as number | null) ?? null);
  const tick = $derived((getAt(payload, "local", "hardware", "tick_interval") as number | null) ?? null);
  function setLocalStr(table: string, key: string, v: string): void {
    payload = v.trim() === "" ? removeAt(payload, "local", table, key) : setAt(payload, "local", table, key, v);
    onChange();
  }
  function setLocalNum(table: string, key: string, v: number | null): void {
    payload = v === null ? removeAt(payload, "local", table, key) : setAt(payload, "local", table, key, v);
    onChange();
  }
</script>

<h2>Deck{#if overlay} · overlay: {editProfile}{/if}</h2>
{#if overlay}
  <OverrideField label="grid" state={scState("grid")} inheritedDisplay={hint("grid")} onstate={(s) => setScState("grid", s)}>
    <TextField label="" value={String(scValue("grid") ?? "")} oninput={(v) => setSc("grid", v)} />
  </OverrideField>
  <TriStateListField label="overview_order" state={overrideState(payload, prof, SEC, "overview_order")} list={ovOverviewList()} inheritLabel="Zdědit" inheritHint={`zděděno: ${hint("overview_order")}`} onchange={setOvOverview} />
{:else}
  <TextField label="grid" value={grid} oninput={(v) => setBase("grid", v)} />
  <TriStateListField label="overview_order" state={overviewState} list={overviewOrder} defaultHint={serverHint} onchange={setBaseOverview} />
{/if}

<fieldset class="hw">
  <legend>Hardware (tento stroj — local.toml)</legend>
  <p class="hint">Platí jen pro tento počítač; nikdy se nepřenáší do profilů ani base configu (ani v overlay módu).</p>
  <TextField label="deck" value={hwDeck} oninput={(v) => setLocalStr("local", "deck", v)} />
  <TextField label="herdr_socket" value={hwSocket} oninput={(v) => setLocalStr("local", "herdr_socket", v)} />
  <TextField label="web_bind" value={hwBind} oninput={(v) => setLocalStr("local", "web_bind", v)} />
  <NumberField label="web_port" value={hwPort} int onchange={(v) => setLocalNum("local", "web_port", v)} />
  <TextField label="icons_dir" value={hwIcons} oninput={(v) => setLocalStr("local", "icons_dir", v)} />
  <NumberField label="brightness" value={brightness} int onchange={(v) => setLocalNum("hardware", "brightness", v)} />
  <NumberField label="debounce" value={debounce} step={0.05} onchange={(v) => setLocalNum("hardware", "debounce", v)} />
  <NumberField label="keep_alive_interval" value={keepAlive} step={0.5} onchange={(v) => setLocalNum("hardware", "keep_alive_interval", v)} />
  <NumberField label="tick_interval" value={tick} step={0.05} onchange={(v) => setLocalNum("hardware", "tick_interval", v)} />
</fieldset>

<style>
  h2 { margin: 0 0 8px; }
  .hw { border: 1px solid #2a2a30; border-radius: 6px; margin: 12px 0; padding: 8px 12px; }
  .hw legend { color: #ccc; }
  .hint { color: #888; margin: 0 0 8px; }
</style>
```

- [ ] **Step 2: Verify the build + suite**

Run: `cd desktop && npm run build` → exit 0.
Run: `cd desktop && npx vitest run` → PASS.

- [ ] **Step 3: Commit**

```bash
git add desktop/src/lib/sections/DeckSection.svelte
git commit -m "feat: DeckSection overlay-aware (grid/overview_order; hardware stays local)"
```

---

### Task 6: SafetySection overlay-aware

**Files:**
- Modify: `desktop/src/lib/sections/SafetySection.svelte`

**Interfaces:**
- Same overlay helpers, `SEC = "safety"`. `approve_always` (bool, OverrideField) + `require_confirm_for` (list, overlay TriStateListField).

- [ ] **Step 1: Rewrite the section**

Replace the entire contents of `desktop/src/lib/sections/SafetySection.svelte` with:

```svelte
<script lang="ts">
  import BooleanField from "../fields/BooleanField.svelte";
  import TriStateListField from "../fields/TriStateListField.svelte";
  import OverrideField from "../fields/OverrideField.svelte";
  import {
    getAt, setAt, listFieldState, setListField,
    inheritedFor, overrideState, overrideValue, setOverride, clearOverride,
    type ListFieldState, type ConfigPayload,
  } from "../configClient";

  let { payload = $bindable(), onChange, editProfile = null }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; editProfile?: string | null } = $props();

  const SEC = "safety";
  const overlay = $derived(editProfile != null && editProfile !== "default");
  const prof = $derived(editProfile ?? "");

  const approveAlways = $derived((getAt(payload, "base", SEC, "approve_always") as boolean) ?? true);
  const requireConfirmFor = $derived((getAt(payload, "base", SEC, "require_confirm_for") as string[]) ?? []);
  const rcfState = $derived(listFieldState(payload, "base", SEC, "require_confirm_for"));
  function set(key: string, value: unknown): void { payload = setAt(payload, "base", SEC, key, value); onChange(); }
  function setBaseRcf(state: ListFieldState, list: string[]): void { payload = setListField(payload, "base", SEC, "require_confirm_for", state, list); onChange(); }

  function hint(key: string): string { const v = inheritedFor(payload, prof, SEC, key); return Array.isArray(v) ? v.join(" · ") : v == null ? "(nic)" : String(v); }
  function scState(key: string): "inherit" | "override" { return overrideState(payload, prof, SEC, key) === "default" ? "inherit" : "override"; }
  function scValue(key: string): unknown { const v = overrideValue(payload, prof, SEC, key); return v === undefined ? inheritedFor(payload, prof, SEC, key) : v; }
  function setScState(key: string, s: "inherit" | "override"): void {
    payload = { ...payload, profiles: s === "inherit" ? clearOverride(payload.profiles, prof, SEC, key) : setOverride(payload.profiles, prof, SEC, key, inheritedFor(payload, prof, SEC, key)) };
    onChange();
  }
  function setSc(key: string, v: unknown): void { payload = { ...payload, profiles: setOverride(payload.profiles, prof, SEC, key, v) }; onChange(); }
  function ovRcfList(): string[] { const v = overrideValue(payload, prof, SEC, "require_confirm_for"); return Array.isArray(v) ? v as string[] : []; }
  function setOvRcf(state: ListFieldState, list: string[]): void {
    payload = { ...payload, profiles: state === "default" ? clearOverride(payload.profiles, prof, SEC, "require_confirm_for") : setOverride(payload.profiles, prof, SEC, "require_confirm_for", state === "empty" ? [] : list) };
    onChange();
  }
</script>

<h2>Safety{#if overlay} · overlay: {editProfile}{/if}</h2>
{#if overlay}
  <OverrideField label="approve_always" state={scState("approve_always")} inheritedDisplay={hint("approve_always")} onstate={(s) => setScState("approve_always", s)}>
    <BooleanField label="" value={Boolean(scValue("approve_always"))} onchange={(v) => setSc("approve_always", v)} />
  </OverrideField>
  <TriStateListField label="require_confirm_for" state={overrideState(payload, prof, SEC, "require_confirm_for")} list={ovRcfList()} inheritLabel="Zdědit" inheritHint={`zděděno: ${hint("require_confirm_for")}`} onchange={setOvRcf} />
{:else}
  <BooleanField label="approve_always" value={approveAlways} onchange={(v) => set("approve_always", v)} />
  <TriStateListField label="require_confirm_for" state={rcfState} list={requireConfirmFor} onchange={setBaseRcf} />
{/if}

<style>
  h2 { margin: 0 0 8px; }
</style>
```

> Note: base `require_confirm_for` adopts the α tri-state here too (its backend default is `[]`, so the three states collapse to default≡empty visually, but using TriStateListField keeps base/overlay rendering uniform and is harmless — the prior plain `ListField` + `putList` is replaced).

- [ ] **Step 2: Verify the build + suite**

Run: `cd desktop && npm run build` → exit 0.
Run: `cd desktop && npx vitest run` → PASS.

- [ ] **Step 3: Commit**

```bash
git add desktop/src/lib/sections/SafetySection.svelte
git commit -m "feat: SafetySection overlay-aware (approve_always/require_confirm_for)"
```

---

### Task 7: ThemeSection overlay-aware (per-status colors)

**Files:**
- Modify: `desktop/src/lib/sections/ThemeSection.svelte`

**Interfaces:**
- Consumes the **path** helpers (Task 1): `inheritedForPath`, `overrideValuePath`, `setOverridePath`, `clearOverridePath` — because a per-status color override lives at `profiles[X].theme.colors.<status>` (2 levels under the section). `server_accents` (a list) uses the regular 2-level overlay helpers. Each status color is a scalar in an `OverrideField`.

- [ ] **Step 1: Rewrite the section**

Replace the entire contents of `desktop/src/lib/sections/ThemeSection.svelte` with:

```svelte
<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import TriStateListField from "../fields/TriStateListField.svelte";
  import OverrideField from "../fields/OverrideField.svelte";
  import {
    getAt, setAt, listFieldState, setListField,
    inheritedFor, inheritedForPath, overrideValue, overrideValuePath, overrideState,
    setOverride, clearOverride, setOverridePath, clearOverridePath,
    type ListFieldState, type ConfigPayload,
  } from "../configClient";

  let { payload = $bindable(), onChange, editProfile = null }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; editProfile?: string | null } = $props();

  const SEC = "theme";
  const STATUS = ["working", "idle", "blocked", "done", "unknown", "offline"];
  const overlay = $derived(editProfile != null && editProfile !== "default");
  const prof = $derived(editProfile ?? "");

  // --- base mode (unchanged) ---
  function baseColorOf(key: string): string {
    return (getAt(payload, "base", SEC, "colors") as Record<string, unknown> | undefined)?.[key] as string ?? "";
  }
  const accents = $derived((getAt(payload, "base", SEC, "server_accents") as string[]) ?? []);
  const accentsState = $derived(listFieldState(payload, "base", SEC, "server_accents"));
  function setBaseColor(key: string, v: string): void {
    const cur = getAt(payload, "base", SEC, "colors");
    const colors: Record<string, unknown> = cur != null && typeof cur === "object" && !Array.isArray(cur) ? { ...(cur as Record<string, unknown>) } : {};
    if (v.trim() === "") delete colors[key]; else colors[key] = v;
    payload = setAt(payload, "base", SEC, "colors", colors);
    onChange();
  }
  function setBaseAccents(state: ListFieldState, list: string[]): void { payload = setListField(payload, "base", SEC, "server_accents", state, list); onChange(); }

  // --- overlay: per-status colors via path helpers (profiles[X].theme.colors.<status>) ---
  function colorPath(status: string): string[] { return [SEC, "colors", status]; }
  function colorInheritedHint(status: string): string { const v = inheritedForPath(payload, prof, colorPath(status)); return v == null ? "(nic)" : String(v); }
  function colorState(status: string): "inherit" | "override" { return overrideValuePath(payload, prof, colorPath(status)) === undefined ? "inherit" : "override"; }
  function colorValue(status: string): string { const v = overrideValuePath(payload, prof, colorPath(status)); return v === undefined ? String(inheritedForPath(payload, prof, colorPath(status)) ?? "") : String(v); }
  function setColorState(status: string, s: "inherit" | "override"): void {
    payload = { ...payload, profiles: s === "inherit" ? clearOverridePath(payload.profiles, prof, colorPath(status)) : setOverridePath(payload.profiles, prof, colorPath(status), inheritedForPath(payload, prof, colorPath(status)) ?? "") };
    onChange();
  }
  function setColor(status: string, v: string): void { payload = { ...payload, profiles: setOverridePath(payload.profiles, prof, colorPath(status), v) }; onChange(); }

  // --- overlay: server_accents (regular 2-level list) ---
  function accentHint(): string { const v = inheritedFor(payload, prof, SEC, "server_accents"); return Array.isArray(v) ? v.join(" · ") : v == null ? "(nic)" : String(v); }
  function ovAccents(): string[] { const v = overrideValue(payload, prof, SEC, "server_accents"); return Array.isArray(v) ? v as string[] : []; }
  function setOvAccents(state: ListFieldState, list: string[]): void {
    payload = { ...payload, profiles: state === "default" ? clearOverride(payload.profiles, prof, SEC, "server_accents") : setOverride(payload.profiles, prof, SEC, "server_accents", state === "empty" ? [] : list) };
    onChange();
  }
</script>

<h2>Theme{#if overlay} · overlay: {editProfile}{/if}</h2>
<fieldset class="colors">
  <legend>colors</legend>
  {#if overlay}
    {#each STATUS as key (key)}
      <OverrideField label={key} state={colorState(key)} inheritedDisplay={colorInheritedHint(key)} onstate={(s) => setColorState(key, s)}>
        <TextField label="" value={colorValue(key)} oninput={(v) => setColor(key, v)} />
      </OverrideField>
    {/each}
  {:else}
    {#each STATUS as key (key)}
      <TextField label={key} value={baseColorOf(key)} oninput={(v) => setBaseColor(key, v)} />
    {/each}
  {/if}
</fieldset>
{#if overlay}
  <TriStateListField label="server_accents" state={overrideState(payload, prof, SEC, "server_accents")} list={ovAccents()} inheritLabel="Zdědit" inheritHint={`zděděno: ${accentHint()}`} onchange={setOvAccents} />
{:else}
  <TriStateListField label="server_accents" state={accentsState} list={accents} onchange={setBaseAccents} />
{/if}

<style>
  h2 { margin: 0 0 8px; }
  .colors { border: 1px solid #2a2a30; border-radius: 6px; margin: 8px 0; padding: 8px 12px; }
  .colors legend { color: #ccc; }
</style>
```

> Note: base `server_accents` moves from `putList` to the α tri-state (`setListField`/`listFieldState`) for base/overlay uniformity; its backend default is non-empty (`DEFAULT_SERVER_ACCENTS`), so default≠empty is meaningful here.

- [ ] **Step 2: Verify the build + suite**

Run: `cd desktop && npm run build` → exit 0.
Run: `cd desktop && npx vitest run` → PASS.

- [ ] **Step 3: Commit**

```bash
git add desktop/src/lib/sections/ThemeSection.svelte
git commit -m "feat: ThemeSection overlay-aware (per-status colors + server_accents)"
```

---

### Task 8: ConfigApp wiring — editProfile + non-Tier-1 note

**Files:**
- Modify: `desktop/src/ConfigApp.svelte`

**Interfaces:**
- Computes `editProfile` from `payload.activeProfile` (named profile → overlay; "default" → base) and passes it to the four Tier-1 sections (View/Deck/Safety/Theme). For the non-Tier-1 sections, when a named profile is active, render a note that they still edit base (β2 will add their overlay).

- [ ] **Step 1: Add the editProfile derived + the note constant**

In `desktop/src/ConfigApp.svelte`, after the `activeValue`/`switcherDisabled` deriveds (~line 56), add:

```ts
  // The profile whose OVERLAY the Tier-1 sections edit (β1). "default" → base mode.
  const editProfile = $derived(payload && payload.activeProfile !== "default" ? payload.activeProfile : null);
  // _OVERLAY_SECTIONS whose overlay editing is not built yet (řez β2): when a profile is
  // active they still edit BASE — warn so the user doesn't think they edit the profile.
  // Servers (base server list) and Profiles (meta-section) are NOT per-section overlays, so
  // they get no note.
  const BASE_ONLY_IN_OVERLAY = ["Macros", "Start profiles", "Notifications", "Answer profiles"];
```

- [ ] **Step 2: Pass editProfile to the Tier-1 sections + add the note**

In the form area, change the four Tier-1 section tags to pass `editProfile`, and add the note before the section block. Replace
```svelte
      {:else if active === "Deck"}
        <DeckSection bind:payload onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else if active === "View"}
        <ViewSection bind:payload onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else if active === "Theme"}
        <ThemeSection bind:payload onChange={markDirty} onError={(m) => setBanner("error", m)} />
```
with
```svelte
      {:else if active === "Deck"}
        <DeckSection bind:payload {editProfile} onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else if active === "View"}
        <ViewSection bind:payload {editProfile} onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else if active === "Theme"}
        <ThemeSection bind:payload {editProfile} onChange={markDirty} onError={(m) => setBanner("error", m)} />
```
And change the Safety line
```svelte
      {:else if active === "Safety"}
        <SafetySection bind:payload onChange={markDirty} onError={(m) => setBanner("error", m)} />
```
to
```svelte
      {:else if active === "Safety"}
        <SafetySection bind:payload {editProfile} onChange={markDirty} onError={(m) => setBanner("error", m)} />
```

Then add the base-only note: immediately after the opening `<section class="form">` line's first child guard — specifically, right after the line
```svelte
      {#if payload == null}
        <p class="hint">Načítám config… (nebo sidecar zatím neběží)</p>
```
insert a new `{:else if}` is not correct here; instead wrap the note ABOVE the section switch. Replace the block opening
```svelte
    <section class="form">
      {#if payload == null}
        <p class="hint">Načítám config… (nebo sidecar zatím neběží)</p>
```
with
```svelte
    <section class="form">
      {#if editProfile && BASE_ONLY_IN_OVERLAY.includes(active)}
        <p class="overlaynote">⚠ Tato sekce zatím edituje <strong>base</strong> (overlay editace profilu „{editProfile}" přijde v řezu β2).</p>
      {/if}
      {#if payload == null}
        <p class="hint">Načítám config… (nebo sidecar zatím neběží)</p>
```

- [ ] **Step 3: Add the note style**

In the `<style>` block of `ConfigApp.svelte`, add next to `.hint`:

```css
  .overlaynote { color: #e0a030; background: #2a2410; border: 1px solid #4a3a10; border-radius: 4px; padding: 6px 10px; margin: 0 0 12px; }
```

- [ ] **Step 4: Verify the build + suite**

Run: `cd desktop && npm run build` → exit 0.
Run: `cd desktop && npx vitest run` → PASS.

- [ ] **Step 5: Commit**

```bash
git add desktop/src/ConfigApp.svelte
git commit -m "feat: ConfigApp wires editProfile to Tier-1 sections + base-only overlay note"
```

---

## Self-Review (run after the plan, before execution)

**Spec coverage:** overlay mechanism (Task 1 chain-aware resolvers + path mutators; Task 2 OverrideField; Task 3 TriStateListField overlay mode) + the four Tier-1 sections (Tasks 4–7) + unified switcher wiring & non-Tier-1 note (Task 8). Tier-2/3 overlay, map-level explicit-empty, and klik-to-jump are spec non-goals (→ β2).

**Type consistency:** `inheritedFor`/`overrideState`/`overrideValue`/`setOverride`/`clearOverride` (2-level) and `inheritedForPath`/`overrideValuePath`/`setOverridePath`/`clearOverridePath` (n-level) signatures match across Tasks 1, 4–7; `OverrideField` props `{label, state:"inherit"|"override", inheritedDisplay, onstate, children}` identical at every call site; `TriStateListField` gains optional `inheritLabel`/`inheritHint` (Task 3) used by Tasks 4–7; `editProfile?: string | null` prop identical across the four sections + ConfigApp (Task 8).

**No placeholders:** every step carries exact file paths, full code, and exact commands. Base-mode rendering in each section is preserved verbatim in the `{:else}` branch (no regression); `setOverride`/`clearOverride` are the řez-3 2-level helpers, `setOverridePath`/`clearOverridePath` the new n-level ones for `theme.colors`.
