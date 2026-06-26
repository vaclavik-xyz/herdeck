# Config editor frontend řez 4b-ii-α (base-mode three-state) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a reusable tri-state (default / custom / empty) list control + pure configClient helpers, and adopt it in base-mode sections so the user can author intentional explicit-`[]` for list keys with a non-empty backend default (turn a tile line off, empty the overview, etc.).

**Architecture:** Thin frontend over the existing config model. A pure `listFieldState`/`setListField` pair (composed from the tested `getAt`/`setAt`/`removeAt`) carries the three model states; a `TriStateListField.svelte` widget wraps the existing `ListField` with a segmented [Výchozí | Vlastní | Vypnuto] control. Four sections swap their list fields to it. No backend change, no new routes/commands/deps.

**Tech Stack:** Svelte 5 (runes), TypeScript, Vitest. Desktop app under `desktop/`.

**Spec:** `docs/superpowers/specs/2026-06-26-config-editor-frontend-4b-ii-alpha-design.md`

## Global Constraints

- **absent ≠ empty:** an ABSENT list key = backend default; explicit `[]` = "none" (default disabled). The whole point of α is to make `[]` *authorable* for adopted fields — `setListField("default", …)` OMITS the key, `setListField("empty", …)` writes `[]`. Do NOT reintroduce omit-on-empty for adopted fields.
- **Model has exactly three distinguishable list states:** absent / `[]` / non-empty. A "custom" list emptied to `[]` is written as `[]` and reads back as "empty" (no fourth state).
- **Svelte 5 runes** only (`$props`, `$derived`, `$bindable`, `$state`, `$effect`). Sections receive `payload = $bindable()`.
- **Component verification = build gate.** Run `cd desktop && npm run build` (exit 0) and `cd desktop && npx vitest run`. There is NO `svelte-check` dependency — do not add or invoke it.
- **Token never in JS; secret values one-way.** NotificationsSection touches secrets — preserve its existing `setSecret`/`clearSecret`/telegram behavior untouched; only its `on`/`backends` list fields change.
- **Czech UI copy, English code/identifiers/commits.** Conventional commits (`feat:`/`refactor:`/`test:`). No `Co-Authored-By`.
- **Test runners:** desktop = `cd desktop && npx vitest run` (single file: `npx vitest run src/lib/configClient.test.ts`) / `npm run build`. (`cargo`/`pytest` are not needed — α is frontend-only.)

---

### Task 1: configClient `listFieldState` + `setListField`

**Files:**
- Modify: `desktop/src/lib/configClient.ts` (add after `putList`, ~line 395)
- Test: `desktop/src/lib/configClient.test.ts` (add a new `describe` block)

**Interfaces:**
- Consumes: existing `ConfigPayload`, `ConfigRoot`, `getAt`, `setAt`, `removeAt` (already in this file).
- Produces:
  - `type ListFieldState = "default" | "custom" | "empty"`
  - `function listFieldState(payload: ConfigPayload, root: ConfigRoot, section: string, key: string): ListFieldState`
  - `function setListField(payload: ConfigPayload, root: ConfigRoot, section: string, key: string, state: ListFieldState, list: string[]): ConfigPayload`

- [ ] **Step 1: Write the failing tests**

Add to `desktop/src/lib/configClient.test.ts`. Add `listFieldState` and `setListField` to the existing top `import { … } from "./configClient"` list, then append:

```ts
describe("listFieldState / setListField", () => {
  it("reads the tri-state of a list key (absent / [] / non-empty)", () => {
    const def = parseConfig({ base: { view: {} } })!;
    expect(listFieldState(def, "base", "view", "tile_primary")).toBe("default");
    const empty = parseConfig({ base: { view: { tile_primary: [] } } })!;
    expect(listFieldState(empty, "base", "view", "tile_primary")).toBe("empty");
    const custom = parseConfig({ base: { view: { tile_primary: ["repo"] } } })!;
    expect(listFieldState(custom, "base", "view", "tile_primary")).toBe("custom");
  });

  it("writes 'default' by OMITTING the key (input untouched)", () => {
    const c = parseConfig({ base: { view: { tile_primary: ["repo"] } } })!;
    const next = setListField(c, "base", "view", "tile_primary", "default", []);
    expect("tile_primary" in (next.base.view as Record<string, unknown>)).toBe(false);
    expect(c.base.view).toEqual({ tile_primary: ["repo"] });
  });

  it("writes 'empty' as an explicit []", () => {
    const c = parseConfig({ base: { view: {} } })!;
    const next = setListField(c, "base", "view", "tile_primary", "empty", []);
    expect((next.base.view as Record<string, unknown>).tile_primary).toEqual([]);
  });

  it("writes 'custom' as the list", () => {
    const c = parseConfig({ base: { view: {} } })!;
    const next = setListField(c, "base", "view", "tile_primary", "custom", ["repo", "branch"]);
    expect((next.base.view as Record<string, unknown>).tile_primary).toEqual(["repo", "branch"]);
  });

  it("round-trips every state through setListField → listFieldState", () => {
    const c = parseConfig({ base: { deck: {} } })!;
    for (const s of ["default", "empty", "custom"] as const) {
      const list = s === "custom" ? ["a"] : [];
      const next = setListField(c, "base", "deck", "overview_order", s, list);
      expect(listFieldState(next, "base", "deck", "overview_order")).toBe(s);
    }
  });

  it("treats a 'custom' write with an empty list as empty on read-back", () => {
    const c = parseConfig({ base: { view: {} } })!;
    const next = setListField(c, "base", "view", "tile_primary", "custom", []);
    expect(listFieldState(next, "base", "view", "tile_primary")).toBe("empty");
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd desktop && npx vitest run src/lib/configClient.test.ts`
Expected: FAIL — `listFieldState`/`setListField` are not exported (import / reference errors).

- [ ] **Step 3: Implement the helpers**

In `desktop/src/lib/configClient.ts`, immediately after the `putList` function (ends ~line 395), add:

```ts
/** The tri-state of a list key: absent → "default" (backend default applies),
 *  `[]` → "empty" (explicit none, default disabled), non-empty → "custom". */
export type ListFieldState = "default" | "custom" | "empty";

/** Read a list key's tri-state. A missing key (any level absent) is "default";
 *  an empty array is "empty"; anything else present is "custom". */
export function listFieldState(
  payload: ConfigPayload,
  root: ConfigRoot,
  section: string,
  key: string,
): ListFieldState {
  const v = getAt(payload, root, section, key);
  if (v === undefined) return "default";
  return Array.isArray(v) && v.length === 0 ? "empty" : "custom";
}

/** NEW payload writing the chosen tri-state for a list key: "default" OMITS the
 *  key (removeAt → backend default), "empty" writes an explicit `[]`, "custom"
 *  writes `list` (a "custom" list that is empty is written as `[]` and reads back
 *  as "empty"). Composes the tested setAt/removeAt; input untouched. */
export function setListField(
  payload: ConfigPayload,
  root: ConfigRoot,
  section: string,
  key: string,
  state: ListFieldState,
  list: string[],
): ConfigPayload {
  if (state === "default") return removeAt(payload, root, section, key);
  if (state === "empty") return setAt(payload, root, section, key, []);
  return setAt(payload, root, section, key, list);
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd desktop && npx vitest run src/lib/configClient.test.ts`
Expected: PASS (all new + all pre-existing).

- [ ] **Step 5: Commit**

```bash
git add desktop/src/lib/configClient.ts desktop/src/lib/configClient.test.ts
git commit -m "feat: configClient listFieldState + setListField tri-state helpers"
```

---

### Task 2: `TriStateListField.svelte` widget

**Files:**
- Create: `desktop/src/lib/fields/TriStateListField.svelte`
- Test: `desktop/src/lib/fields/widgets.smoke.test.ts` (add compile-smoke)

**Interfaces:**
- Consumes: `ListFieldState` (Task 1); existing `ListField.svelte`.
- Produces: a widget with props
  `{ label: string; state: ListFieldState; list: string[]; defaultHint?: string; onchange: (state: ListFieldState, list: string[]) => void }`.
  Sections pass the field's current state+list and call `setListField` in the callback.

- [ ] **Step 1: Create the widget**

Create `desktop/src/lib/fields/TriStateListField.svelte`:

```svelte
<script lang="ts">
  import ListField from "./ListField.svelte";
  import type { ListFieldState } from "../configClient";

  let { label, state, list, defaultHint, onchange }:
    {
      label: string;
      state: ListFieldState;
      list: string[];
      defaultHint?: string;
      onchange: (state: ListFieldState, list: string[]) => void;
    } = $props();

  const SEGMENTS: { value: ListFieldState; text: string }[] = [
    { value: "default", text: "Výchozí" },
    { value: "custom", text: "Vlastní" },
    { value: "empty", text: "Vypnuto" },
  ];

  // Switching to "custom" carries the current list (user then edits it); "default"/"empty"
  // derive their value from the state at write time, so the list is just passed through.
  function pick(next: ListFieldState): void {
    if (next !== state) onchange(next, list);
  }
</script>

<div class="tristate">
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
    {#if state === "custom"}
      <ListField label="" value={list} onchange={(v) => onchange("custom", v)} />
    {:else if state === "default"}
      <p class="hint">{defaultHint ? `výchozí: ${defaultHint}` : "(výchozí)"}</p>
    {:else}
      <p class="hint">prázdné — vypnuto</p>
    {/if}
  </div>
</div>

<style>
  .tristate { display: grid; grid-template-columns: 120px 1fr; align-items: start; gap: 8px; margin: 6px 0; }
  .label { color: #aaa; padding-top: 4px; }
  .body { display: flex; flex-direction: column; gap: 4px; }
  .seg { display: inline-flex; align-self: flex-start; border: 1px solid #2a2a30; border-radius: 4px; overflow: hidden; }
  .seg button { background: #141417; border: 0; border-right: 1px solid #2a2a30; color: #aaa; padding: 4px 10px; cursor: pointer; }
  .seg button:last-child { border-right: 0; }
  .seg button.on { background: #2a2a30; color: #e8e8ea; }
  .hint { color: #777; margin: 2px 0; font-style: italic; }
</style>
```

- [ ] **Step 2: Add the compile-smoke test (and run it to verify it fails first)**

In `desktop/src/lib/fields/widgets.smoke.test.ts`, add the import and a case:

```ts
import TriStateListField from "./TriStateListField.svelte";
```
```ts
  it("compiles TriStateListField", () => {
    expect(TriStateListField).toBeTruthy();
  });
```

Run (BEFORE creating the file, if doing strict TDD; otherwise this validates compilation): `cd desktop && npx vitest run src/lib/fields/widgets.smoke.test.ts`
Expected after Step 1: PASS (the widget compiles and imports).

- [ ] **Step 3: Verify the build**

Run: `cd desktop && npm run build`
Expected: exit 0 (no Svelte/TS compile errors). Pre-existing cosmetic `state_referenced_locally` warnings are acceptable (build still exits 0).

- [ ] **Step 4: Commit**

```bash
git add desktop/src/lib/fields/TriStateListField.svelte desktop/src/lib/fields/widgets.smoke.test.ts
git commit -m "feat: TriStateListField widget (segmented Výchozí/Vlastní/Vypnuto)"
```

---

### Task 3: ViewSection adopts tri-state

**Files:**
- Modify: `desktop/src/lib/sections/ViewSection.svelte`

**Interfaces:**
- Consumes: `listFieldState`, `setListField`, `ListFieldState` (Task 1); `TriStateListField` (Task 2); existing `getAt`, `setAt`.
- The four list fields `bottom_row`, `tile_fields`, `tile_primary`, `tile_secondary` move from `putList` to `setListField` + `TriStateListField`. Scalars (`management`, `agent_slots`, `show_profile_on_panel`) are unchanged.

- [ ] **Step 1: Rewrite the section**

Replace the entire contents of `desktop/src/lib/sections/ViewSection.svelte` with:

```svelte
<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import SelectField from "../fields/SelectField.svelte";
  import BooleanField from "../fields/BooleanField.svelte";
  import TriStateListField from "../fields/TriStateListField.svelte";
  import { getAt, setAt, listFieldState, setListField, type ListFieldState, type ConfigPayload } from "../configClient";

  let { payload = $bindable(), onChange }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  const MANAGEMENT = ["launcher_menu", "bottom_row"];

  const management = $derived((getAt(payload, "base", "view", "management") as string) ?? "launcher_menu");
  const agentSlots = $derived((getAt(payload, "base", "view", "agent_slots") as string) ?? "");
  const showProfile = $derived((getAt(payload, "base", "view", "show_profile_on_panel") as boolean) ?? false);

  // Tri-state list fields: absent → backend default, [] → off, non-empty → custom.
  // state/list are derived inline in the template per key (see {#each}).
  const LIST_KEYS = ["bottom_row", "tile_fields", "tile_primary", "tile_secondary"] as const;

  function set(key: string, value: unknown): void {
    payload = setAt(payload, "base", "view", key, value);
    onChange();
  }
  function setTri(key: string, state: ListFieldState, list: string[]): void {
    payload = setListField(payload, "base", "view", key, state, list);
    onChange();
  }
</script>

<h2>View</h2>
<SelectField label="management" value={management} options={MANAGEMENT} onchange={(v) => set("management", v)} />
<TextField label="agent_slots" value={agentSlots} oninput={(v) => set("agent_slots", v)} />
<BooleanField label="show_profile_on_panel" value={showProfile} onchange={(v) => set("show_profile_on_panel", v)} />
{#each LIST_KEYS as key}
  <TriStateListField
    label={key}
    state={listFieldState(payload, "base", "view", key)}
    list={(getAt(payload, "base", "view", key) as string[]) ?? []}
    onchange={(s, l) => setTri(key, s, l)}
  />
{/each}

<style>
  h2 { margin: 0 0 8px; }
</style>
```

- [ ] **Step 2: Verify the build**

Run: `cd desktop && npm run build`
Expected: exit 0.

- [ ] **Step 3: Verify the suite still passes**

Run: `cd desktop && npx vitest run`
Expected: PASS (no logic regressions; component change is build-gated).

- [ ] **Step 4: Commit**

```bash
git add desktop/src/lib/sections/ViewSection.svelte
git commit -m "feat: ViewSection list fields adopt tri-state (authorable explicit-empty)"
```

---

### Task 4: DeckSection + NotificationsSection adopt tri-state

**Files:**
- Modify: `desktop/src/lib/sections/DeckSection.svelte` (`overview_order`)
- Modify: `desktop/src/lib/sections/NotificationsSection.svelte` (`on`, `backends`)

**Interfaces:**
- Consumes: `listFieldState`, `setListField`, `ListFieldState`, `serversOf` (configClient); `TriStateListField`.
- DeckSection: `overview_order` gets a `defaultHint` = base server ids joined (its backend default is "all servers in order"). Hardware (local.toml) fields are unchanged.
- NotificationsSection: `on`/`backends` swap from `putList`; secrets/telegram code is untouched.

- [ ] **Step 1: Edit DeckSection**

In `desktop/src/lib/sections/DeckSection.svelte`:

Change the import line
```ts
  import { getAt, setAt, removeAt, putList, type ConfigPayload } from "../configClient";
```
to
```ts
  import { getAt, setAt, removeAt, listFieldState, setListField, serversOf, type ListFieldState, type ConfigPayload } from "../configClient";
```
Add the `TriStateListField` import next to the other field imports:
```ts
  import TriStateListField from "../fields/TriStateListField.svelte";
```

Replace
```ts
  const overviewOrder = $derived((getAt(payload, "base", "deck", "overview_order") as string[]) ?? []);
```
with
```ts
  const overviewOrder = $derived((getAt(payload, "base", "deck", "overview_order") as string[]) ?? []);
  const overviewState = $derived(listFieldState(payload, "base", "deck", "overview_order"));
  const overviewHint = $derived(serversOf(payload).map((s) => s.id).filter((id) => id !== "").join(" · "));
```

Replace the `setOverviewOrder` function
```ts
  // overview_order is a list: empty → omit (backend default = all servers), not an empty selection.
  function setOverviewOrder(list: string[]): void {
    payload = putList(payload, "base", "deck", "overview_order", list);
    onChange();
  }
```
with
```ts
  // overview_order tri-state: absent → all servers (default), [] → empty overview, custom → list.
  function setOverviewOrder(state: ListFieldState, list: string[]): void {
    payload = setListField(payload, "base", "deck", "overview_order", state, list);
    onChange();
  }
```

Replace the template line
```svelte
<ListField label="overview_order" value={overviewOrder} onchange={setOverviewOrder} />
```
with
```svelte
<TriStateListField label="overview_order" state={overviewState} list={overviewOrder} defaultHint={overviewHint} onchange={setOverviewOrder} />
```
Remove the now-unused `ListField` import (DeckSection no longer uses it):
```ts
  import ListField from "../fields/ListField.svelte";
```
delete that line.

- [ ] **Step 2: Edit NotificationsSection**

In `desktop/src/lib/sections/NotificationsSection.svelte`:

Change the import block
```ts
  import {
    commandTransport as cfgTransport, getAt, setAt, removeAt, putList, secretFlag, type ConfigPayload,
  } from "../configClient";
```
to
```ts
  import {
    commandTransport as cfgTransport, getAt, setAt, removeAt, listFieldState, setListField,
    secretFlag, type ListFieldState, type ConfigPayload,
  } from "../configClient";
```
Replace the `ListField` import
```ts
  import ListField from "../fields/ListField.svelte";
```
with
```ts
  import TriStateListField from "../fields/TriStateListField.svelte";
```

Replace
```ts
  const on = $derived((getAt(payload, "base", "notifications", "on") as string[]) ?? []);
  const backends = $derived((getAt(payload, "base", "notifications", "backends") as string[]) ?? []);
```
with
```ts
  const on = $derived((getAt(payload, "base", "notifications", "on") as string[]) ?? []);
  const onState = $derived(listFieldState(payload, "base", "notifications", "on"));
  const backends = $derived((getAt(payload, "base", "notifications", "backends") as string[]) ?? []);
  const backendsState = $derived(listFieldState(payload, "base", "notifications", "backends"));
```

Replace the `setList` function
```ts
  // `on`/`backends` are lists: empty → omit (backend defaults ["blocked"]/["macos"]), not [].
  function setList(key: string, list: string[]): void {
    payload = putList(payload, "base", "notifications", key, list);
    onChange();
  }
```
with
```ts
  // `on`/`backends` tri-state: absent → backend defaults (["blocked"]/["macos"]), [] → none, custom → list.
  function setTri(key: string, state: ListFieldState, list: string[]): void {
    payload = setListField(payload, "base", "notifications", key, state, list);
    onChange();
  }
```

Replace the two template lines
```svelte
<ListField label="on" value={on} onchange={(v) => setList("on", v)} />
<ListField label="backends" value={backends} onchange={(v) => setList("backends", v)} />
```
with
```svelte
<TriStateListField label="on" state={onState} list={on} onchange={(s, l) => setTri("on", s, l)} />
<TriStateListField label="backends" state={backendsState} list={backends} onchange={(s, l) => setTri("backends", s, l)} />
```

- [ ] **Step 3: Verify the build + suite**

Run: `cd desktop && npm run build` → exit 0.
Run: `cd desktop && npx vitest run` → PASS.

- [ ] **Step 4: Commit**

```bash
git add desktop/src/lib/sections/DeckSection.svelte desktop/src/lib/sections/NotificationsSection.svelte
git commit -m "feat: Deck overview_order + Notifications on/backends adopt tri-state"
```

---

### Task 5: AnswerProfilesSection `approve_always` tri-state

**Files:**
- Modify: `desktop/src/lib/sections/AnswerProfilesSection.svelte`

**Interfaces:**
- Consumes: `TriStateListField` (Task 2); `ListFieldState` (Task 1); existing `AnswerProfileRow` (its `approve_always: string[] | null` already distinguishes absent/empty/custom), `serializeNamedRows` (already maps `null` → omit, `[]` → include `[]`, list → include).
- `approve`/`deny`/`stop` stay as `ListField`. `approve_always` becomes a `TriStateListField` driven by the row field: `null` ↔ "default", `[]` ↔ "empty", non-empty ↔ "custom". The `commit`/serialize path is unchanged (it already handles all three).

- [ ] **Step 1: Edit the section**

In `desktop/src/lib/sections/AnswerProfilesSection.svelte`:

Add the import next to `ListField`:
```ts
  import TriStateListField from "../fields/TriStateListField.svelte";
  import type { ListFieldState } from "../configClient";
```

Change the keys constant
```ts
  const KEYS = ["approve", "deny", "stop", "approve_always"] as const;
```
to
```ts
  const LIST_KEYS = ["approve", "deny", "stop"] as const;
```

Add two helpers next to `setList` (the existing `setList`/`rename`/`add`/`remove` stay; only `setList`'s key type changes since `KEYS` is gone):

Replace
```ts
  function rename(i: number, name: string): void { commit(rows.map((r, j) => (j === i ? { ...r, name } : r))); }
  function setList(i: number, key: (typeof KEYS)[number], v: string[]): void {
    commit(rows.map((r, j) => (j === i ? { ...r, [key]: v } : r)));
  }
```
with
```ts
  function rename(i: number, name: string): void { commit(rows.map((r, j) => (j === i ? { ...r, name } : r))); }
  function setList(i: number, key: (typeof LIST_KEYS)[number], v: string[]): void {
    commit(rows.map((r, j) => (j === i ? { ...r, [key]: v } : r)));
  }
  // approve_always carries a third "default" (null) state: absent → backend falls back to approve.
  function aaState(r: AnswerProfileRow): ListFieldState {
    if (r.approve_always === null) return "default";
    return r.approve_always.length === 0 ? "empty" : "custom";
  }
  function setApproveAlways(i: number, state: ListFieldState, list: string[]): void {
    const value: string[] | null = state === "default" ? null : state === "empty" ? [] : list;
    commit(rows.map((r, j) => (j === i ? { ...r, approve_always: value } : r)));
  }
```

Replace the per-row key loop
```svelte
    {#if e.name.trim() !== ""}
      {#each KEYS as k}
        <ListField label={k} value={e[k] ?? []} onchange={(v) => setList(i, k, v)} />
      {/each}
    {:else}
```
with
```svelte
    {#if e.name.trim() !== ""}
      {#each LIST_KEYS as k}
        <ListField label={k} value={e[k] ?? []} onchange={(v) => setList(i, k, v)} />
      {/each}
      <TriStateListField
        label="approve_always"
        state={aaState(e)}
        list={e.approve_always ?? []}
        onchange={(s, l) => setApproveAlways(i, s, l)}
      />
    {:else}
```

- [ ] **Step 2: Verify the build + suite**

Run: `cd desktop && npm run build` → exit 0.
Run: `cd desktop && npx vitest run` → PASS.

- [ ] **Step 3: Commit**

```bash
git add desktop/src/lib/sections/AnswerProfilesSection.svelte
git commit -m "feat: AnswerProfiles approve_always adopts tri-state (default/none/custom)"
```

---

## Self-Review (run after the plan, before execution)

**Spec coverage:** primitive (Task 1) + widget (Task 2) + the four adopting sections View/Deck/Notifications/AnswerProfiles (Tasks 3–5) cover every "adopting" field in the spec table. `safety.require_confirm_for` is deliberately NOT adopted (default already `[]`). Map-level explicit-empty + overlay + klik-to-jump are spec non-goals (→ β).

**Type consistency:** `ListFieldState` / `listFieldState` / `setListField` signatures match across Tasks 1–5; `TriStateListField` prop shape `{label, state, list, defaultHint?, onchange}` is identical at every call site; `AnswerProfileRow.approve_always: string[] | null` matches `aaState`/`setApproveAlways`.

**No placeholders:** every step carries exact file paths, full code, and exact commands.
