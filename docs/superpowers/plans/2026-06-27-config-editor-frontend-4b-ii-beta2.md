# Config editor frontend řez 4b-ii-β2 (Tier-2/3 overlay + map-level explicit-empty) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Overlay-aware editaci zbylých 4 `_OVERLAY_SECTIONS` (Notifications, Macros, Start profiles, Answer profiles) + map-level explicit-empty (`start_profiles={}`, `profile.servers=[]`), čímž se uzavře Phase 2 frontend overlay editace.

**Architecture:** Tenká vrstva nad backend API. configClient dostane chain-aware mapový resolver (mirror backend `_merge_section`) + path/state helpery; sekce se rozvětví na `overlay` (edituje `profiles[X][section]`) vs base (beze změny). Per-entry override mapových sekcí čte živě z payloadu a zapisuje přes `setOverridePath`/`clearOverridePath`. Žádná Python změna, žádné nové routes/Tauri commandy.

**Tech Stack:** Svelte 5 (runes + snippets), TypeScript, Vitest, Vite build. Reuse β1 widgetů `OverrideField` + overlay `TriStateListField`.

## Global Constraints

- **Token VALUE nikdy v JS / TOML / response / logu** — jen env-var NAME + `{set, source}`. Keyring service literál `"herdeck"`. Secret set/clear operuje na env NAME (globální keychain), per-profil overlay jen autoruje jiné env jméno.
- **Žádná backend (Python) změna.** Žádné nové HTTP routes, žádné nové Tauri commandy, žádné nové runtime závislosti.
- **Base větev každé sekce zůstává byte-equivalentní dnešku** (žádná regrese) — overlay je čistě additivní `{#if overlay}` větev; existující markup jde do `{:else}` verbatim.
- **absent ≠ empty:** ABSENT list/map klíč = backend default; explicit `[]`/`{}` = „none". Overlay „default" segment = INHERIT (zdědit).
- **Default mirrory** backendu nesou `// keep in sync` komentář (frontend nezná backend defaulty → při base-omitu skaláru musí inherit display + override seed resolvnout efektivní default).
- **Per-entry mapový override:** zděděnou položku NELZE v overlay smazat (backend additivní merge to neumí) → honest note, žádný fake-delete.
- Test runnery: Desktop = `cd desktop && npx vitest run` (single file: `npx vitest run src/lib/configClient.test.ts`) / `npm run build` (exit 0; NO svelte-check). β2 je frontend-only (žádný cargo/pytest).
- Komunikace/komentáře v kódu anglicky; UI stringy česky (konzistentně se stávajícími sekcemi).

**Spec:** `docs/superpowers/specs/2026-06-27-config-editor-frontend-4b-ii-beta2-design.md`

**Existing helpers (reuse, already in `configClient.ts`):** `clone`, `asDict` (private), `obj`/`str`/`strList` (private), `getAt`/`setAt`/`removeAt`, `readPath`/`inheritedChain` (private), `inheritedFor`/`inheritedForPath`, `overrideValue`/`overrideValuePath`, `overrideState`, `setOverride`/`clearOverride`, `setOverridePath`/`clearOverridePath`, `MacroRecord`, `macrosOf`, `serializeNamedRows`/`applyMapSection`/`startProfileRows`/`answerProfileRows`, `profileServers`/`setProfileServers`, `secretFlag`, `type ListFieldState = "default" | "custom" | "empty"`. Widgets: `OverrideField` (`{label, state: "inherit"|"override", inheritedDisplay, onstate, children}`), `TriStateListField` (overlay: `inheritLabel`/`inheritHint` props).

---

### Task 1: configClient — chain-aware mapový resolver + path/list helpers

**Files:**
- Modify: `desktop/src/lib/configClient.ts`
- Test: `desktop/src/lib/configClient.test.ts`

**Interfaces:**
- Produces: `mergeSection(base: unknown, overlay: unknown): unknown`; `inheritedSection(payload: ConfigPayload, profile: string, section: string): Record<string, unknown>`; `overrideStatePath(payload: ConfigPayload, profile: string, path: string[]): ListFieldState`; `macroRecords(raw: unknown): MacroRecord[]`. Refactors `macrosOf` to delegate to `macroRecords`.
- Consumes: existing private `clone`, `asDict`, `obj`, `str`, `readPath`, `inheritedChain`; existing `MacroRecord`, `ListFieldState`.

- [ ] **Step 1: Write the failing tests**

Append to `desktop/src/lib/configClient.test.ts` (use the file's existing import line — add `mergeSection, inheritedSection, overrideStatePath, macroRecords` to it):

```ts
describe("mergeSection (mirror backend _merge_section)", () => {
  it("merges two dicts per-key recursively", () => {
    expect(mergeSection({ a: 1, n: { x: 1 } }, { b: 2, n: { y: 2 } })).toEqual({ a: 1, b: 2, n: { x: 1, y: 2 } });
  });
  it("overlay replaces a list/scalar wholesale", () => {
    expect(mergeSection([1, 2], [3])).toEqual([3]);
    expect(mergeSection({ a: 1 }, 5)).toEqual(5);
  });
  it("overlay replaces when base is absent", () => {
    expect(mergeSection(undefined, { a: 1 })).toEqual({ a: 1 });
  });
});

describe("inheritedSection", () => {
  const base = (sp: unknown) => ({
    base: { start_profiles: sp } as Record<string, unknown>,
    profiles: {} as Record<string, Record<string, unknown>>,
    local: {}, secrets: {}, activeProfile: "default", envLocked: false,
  });
  it("returns base section when profile extends default", () => {
    const p: any = base({ codex: ["codex"] }); p.profiles = { dev: {} };
    expect(inheritedSection(p, "dev", "start_profiles")).toEqual({ codex: ["codex"] });
  });
  it("merges parent overlays per-key, excluding the profile's own overlay", () => {
    const p: any = base({ codex: ["codex"] });
    p.profiles = {
      mid: { extends: "default", start_profiles: { claude: ["claude"] } },
      dev: { extends: "mid", start_profiles: { codex: ["codex", "--yolo"] } },
    };
    // base codex + mid's claude; dev's OWN override of codex is excluded
    expect(inheritedSection(p, "dev", "start_profiles")).toEqual({ codex: ["codex"], claude: ["claude"] });
  });
  it("falls back to base on a cycle or unknown extends target", () => {
    const p: any = base({ codex: ["codex"] });
    p.profiles = { dev: { extends: "ghost", start_profiles: { x: ["x"] } } };
    expect(inheritedSection(p, "dev", "start_profiles")).toEqual({ codex: ["codex"] });
  });
  it("returns {} when the section is absent everywhere", () => {
    const p: any = base(undefined); p.profiles = { dev: {} };
    expect(inheritedSection(p, "dev", "start_profiles")).toEqual({});
  });
});

describe("overrideStatePath", () => {
  const p: any = { base: {}, profiles: { dev: { macros: [] } }, local: {}, secrets: {}, activeProfile: "default", envLocked: false };
  it("absent path → default, [] → empty, value → custom", () => {
    expect(overrideStatePath(p, "dev", ["macros"])).toBe("empty");
    expect(overrideStatePath(p, "dev", ["notifications"])).toBe("default");
    const q: any = { ...p, profiles: { dev: { macros: [{ label: "a", text: "b" }] } } };
    expect(overrideStatePath(q, "dev", ["macros"])).toBe("custom");
  });
});

describe("macroRecords", () => {
  it("extracts label/text records, tolerating junk", () => {
    expect(macroRecords([{ label: "a", text: "x" }, {}])).toEqual([{ label: "a", text: "x" }, { label: "", text: "" }]);
    expect(macroRecords(undefined)).toEqual([]);
    expect(macroRecords("nope")).toEqual([]);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd desktop && npx vitest run src/lib/configClient.test.ts`
Expected: FAIL — `mergeSection`/`inheritedSection`/`overrideStatePath`/`macroRecords` are not exported.

- [ ] **Step 3: Implement the helpers**

In `desktop/src/lib/configClient.ts`, **refactor `macrosOf`** to delegate (replace its body):

```ts
/** Extract `{label,text}[]` from any list value (tolerates junk entries). */
export function macroRecords(raw: unknown): MacroRecord[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((m) => {
    const r = obj(m);
    return { label: str(r.label), text: str(r.text) };
  });
}

/** The base `macros` list as editable records (always an array). */
export function macrosOf(payload: ConfigPayload): MacroRecord[] {
  return macroRecords(payload.base.macros);
}
```

Add near the other resolver helpers (after `inheritedSection` site, e.g. just below `overrideState`):

```ts
/** JS mirror of backend `settings._merge_section`: two dicts merge per-key
 *  recursively; a list/scalar overlay (or absent base) replaces wholesale. */
export function mergeSection(base: unknown, overlay: unknown): unknown {
  if (
    base != null && typeof base === "object" && !Array.isArray(base) &&
    overlay != null && typeof overlay === "object" && !Array.isArray(overlay)
  ) {
    const out: Record<string, unknown> = { ...(base as Record<string, unknown>) };
    for (const [k, v] of Object.entries(overlay as Record<string, unknown>)) {
      out[k] = mergeSection(out[k], v);
    }
    return out;
  }
  return overlay;
}

/** The effective map a `profile` INHERITS at `section`: base merged with parent
 *  overlays via `extends` (per-key, mirroring the backend), EXCLUDING the profile's
 *  OWN overlay. A cycle/unknown extends target falls back to base (via `inheritedChain`).
 *  Used to resolve inherited map entries faithfully (e.g. start_profiles, answer_profiles). */
export function inheritedSection(
  payload: ConfigPayload,
  profile: string,
  section: string,
): Record<string, unknown> {
  let merged: unknown = asDict(payload.base)[section];
  for (const overlay of inheritedChain(payload.profiles, profile)) {
    if (section in overlay) merged = mergeSection(merged, overlay[section]);
  }
  return asDict(merged);
}

/** Override state at `path` in `profile`'s OWN overlay (path variant of `overrideState`):
 *  absent → "default" (= inherit), `[]` → "empty", anything else present → "custom". */
export function overrideStatePath(
  payload: ConfigPayload,
  profile: string,
  path: string[],
): ListFieldState {
  const { found, value } = readPath(payload.profiles[profile], path);
  if (!found) return "default";
  return Array.isArray(value) && value.length === 0 ? "empty" : "custom";
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd desktop && npx vitest run src/lib/configClient.test.ts`
Expected: PASS (all new + all pre-existing).

- [ ] **Step 5: Commit**

```bash
git add desktop/src/lib/configClient.ts desktop/src/lib/configClient.test.ts
git commit -m "feat: configClient chain-aware map resolver + path/list helpers (β2 Task 1)"
```

---

### Task 2: configClient — base map-empty + serverless helpers

**Files:**
- Modify: `desktop/src/lib/configClient.ts`
- Test: `desktop/src/lib/configClient.test.ts`

**Interfaces:**
- Produces: `mapSectionState(payload, section): ListFieldState`; `setMapSectionState(payload, section, state): ConfigPayload`; `profileServersState(payload, name): "inherit" | "explicit"`; `setProfileServersExplicit(payload, name, servers: string[]): ConfigPayload`; `clearProfileServers(payload, name): ConfigPayload`.
- Consumes: existing private `clone`, `asDict`.

- [ ] **Step 1: Write the failing tests**

Append to `desktop/src/lib/configClient.test.ts` (add `mapSectionState, setMapSectionState, profileServersState, setProfileServersExplicit, clearProfileServers` to the import line):

```ts
describe("mapSectionState / setMapSectionState (base map explicit-empty)", () => {
  const mk = (sp: unknown): any => ({ base: sp === undefined ? {} : { start_profiles: sp }, profiles: {}, local: {}, secrets: {}, activeProfile: "default", envLocked: false });
  it("absent → default, {} → empty, non-empty → custom", () => {
    expect(mapSectionState(mk(undefined), "start_profiles")).toBe("default");
    expect(mapSectionState(mk({}), "start_profiles")).toBe("empty");
    expect(mapSectionState(mk({ codex: ["codex"] }), "start_profiles")).toBe("custom");
  });
  it("setMapSectionState writes {}, deletes, and leaves custom untouched (immutably)", () => {
    const start = mk({ codex: ["codex"] });
    const empty = setMapSectionState(start, "start_profiles", "empty");
    expect((empty.base as any).start_profiles).toEqual({});
    expect((start.base as any).start_profiles).toEqual({ codex: ["codex"] }); // input untouched
    const def = setMapSectionState(start, "start_profiles", "default");
    expect("start_profiles" in (def.base as any)).toBe(false);
    expect(setMapSectionState(start, "start_profiles", "custom")).toBe(start); // no-op
  });
});

describe("profile servers serverless authoring", () => {
  const mk = (servers: unknown): any => ({ base: {}, profiles: { dev: servers === undefined ? {} : { servers } }, local: {}, secrets: {}, activeProfile: "default", envLocked: false });
  it("profileServersState: absent → inherit, [] or list → explicit", () => {
    expect(profileServersState(mk(undefined), "dev")).toBe("inherit");
    expect(profileServersState(mk([]), "dev")).toBe("explicit");
    expect(profileServersState(mk(["a"]), "dev")).toBe("explicit");
  });
  it("setProfileServersExplicit writes the key even for [] (serverless)", () => {
    const out = setProfileServersExplicit(mk(undefined), "dev", []);
    expect((out.profiles.dev as any).servers).toEqual([]);
    expect(profileServersState(out, "dev")).toBe("explicit");
  });
  it("clearProfileServers omits the key (back to inherit), immutably", () => {
    const start = mk(["a"]);
    const out = clearProfileServers(start, "dev");
    expect("servers" in (out.profiles.dev as any)).toBe(false);
    expect((start.profiles.dev as any).servers).toEqual(["a"]); // input untouched
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd desktop && npx vitest run src/lib/configClient.test.ts`
Expected: FAIL — the five helpers are not exported.

- [ ] **Step 3: Implement the helpers**

Add to `desktop/src/lib/configClient.ts` (near `applyMapSection` for the map ones, near `setProfileServers` for the server ones):

```ts
/** Base `[section]` tri-state: absent → "default" (backend default map), `{}` → "empty"
 *  (explicit none, e.g. no launchers), non-empty dict → "custom". */
export function mapSectionState(payload: ConfigPayload, section: string): ListFieldState {
  const v = (payload.base as Record<string, unknown>)[section];
  if (v === undefined) return "default";
  if (v != null && typeof v === "object" && !Array.isArray(v)) {
    return Object.keys(v as Record<string, unknown>).length === 0 ? "empty" : "custom";
  }
  return "custom";
}

/** NEW payload setting base `[section]` map state: "default" DELETES the key (backend
 *  default), "empty" writes `{}` (explicit none), "custom" is a no-op (the rows editor
 *  populates the map). Input untouched. */
export function setMapSectionState(payload: ConfigPayload, section: string, state: ListFieldState): ConfigPayload {
  if (state === "custom") return payload;
  const base = { ...(payload.base as Record<string, unknown>) };
  if (state === "default") delete base[section];
  else base[section] = {};
  return { ...payload, base };
}

/** Whether profile `name` has an explicit `servers` selection (present, incl. `[]` =
 *  serverless) or inherits base servers (key absent). */
export function profileServersState(payload: ConfigPayload, name: string): "inherit" | "explicit" {
  return "servers" in asDict(payload.profiles[name]) ? "explicit" : "inherit";
}

/** NEW payload writing profile `name`'s `servers` ALWAYS (even `[]` = serverless),
 *  unlike `setProfileServers` which omits an empty list. Input untouched. */
export function setProfileServersExplicit(payload: ConfigPayload, name: string, servers: string[]): ConfigPayload {
  const profiles = clone(payload.profiles);
  const overlay = { ...asDict(profiles[name]) };
  overlay.servers = servers;
  profiles[name] = overlay;
  return { ...payload, profiles };
}

/** NEW payload OMITTING profile `name`'s `servers` key (back to inheriting base). Input untouched. */
export function clearProfileServers(payload: ConfigPayload, name: string): ConfigPayload {
  const profiles = clone(payload.profiles);
  const overlay = { ...asDict(profiles[name]) };
  delete overlay.servers;
  profiles[name] = overlay;
  return { ...payload, profiles };
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd desktop && npx vitest run src/lib/configClient.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add desktop/src/lib/configClient.ts desktop/src/lib/configClient.test.ts
git commit -m "feat: configClient base map-empty + serverless helpers (β2 Task 2)"
```

---

### Task 3: NotificationsSection overlay-aware

**Files:**
- Modify: `desktop/src/lib/sections/NotificationsSection.svelte`

**Interfaces:**
- Consumes: Task 1/β1 helpers; adds `editProfile?: string | null` prop (defaults null → base mode, byte-equivalent to today).

- [ ] **Step 1: Add overlay imports + props + helpers**

In the `<script>`, extend the configClient import with `inheritedFor, inheritedForPath, overrideValue, overrideValuePath, overrideState, setOverride, clearOverride, setOverridePath, clearOverridePath` and add `OverrideField` import:

```ts
import OverrideField from "../fields/OverrideField.svelte";
```

Change props to add `editProfile`:

```ts
let { payload = $bindable(), onChange, onError, editProfile = null }:
  { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; editProfile?: string | null } = $props();

const SEC = "notifications";
const overlay = $derived(editProfile != null && editProfile !== "default");
const prof = $derived(editProfile ?? "");
// Mirror of backend defaults (settings._notifications_config) — keep in sync.
const NOTIF_DEFAULTS: Record<string, boolean> = { enabled: false, sound: true };
const NOTIF_LIST_DEFAULTS: Record<string, string[]> = { on: ["blocked"], backends: ["macos"] };

// --- overlay scalar (enabled/sound) ---
function scHint(key: string): string { const v = inheritedFor(payload, prof, SEC, key); return String(v ?? NOTIF_DEFAULTS[key]); }
function scState(key: string): "inherit" | "override" { return overrideState(payload, prof, SEC, key) === "default" ? "inherit" : "override"; }
function scBool(key: string): boolean { const v = overrideValue(payload, prof, SEC, key); return v === undefined ? Boolean(inheritedFor(payload, prof, SEC, key) ?? NOTIF_DEFAULTS[key]) : Boolean(v); }
function setScState(key: string, s: "inherit" | "override"): void {
  payload = { ...payload, profiles: s === "inherit" ? clearOverride(payload.profiles, prof, SEC, key) : setOverride(payload.profiles, prof, SEC, key, inheritedFor(payload, prof, SEC, key) ?? NOTIF_DEFAULTS[key]) };
  onChange();
}
function setSc(key: string, v: unknown): void { payload = { ...payload, profiles: setOverride(payload.profiles, prof, SEC, key, v) }; onChange(); }

// --- overlay list (on/backends) ---
function listHint(key: string): string { const v = inheritedFor(payload, prof, SEC, key) ?? NOTIF_LIST_DEFAULTS[key]; return Array.isArray(v) ? v.join(" · ") : "(nic)"; }
function ovList(key: string): string[] { const v = overrideValue(payload, prof, SEC, key); return Array.isArray(v) ? (v as string[]) : []; }
function setOvList(key: string, state: ListFieldState, list: string[]): void {
  payload = { ...payload, profiles: state === "default" ? clearOverride(payload.profiles, prof, SEC, key) : setOverride(payload.profiles, prof, SEC, key, state === "empty" ? [] : list) };
  onChange();
}

// --- overlay telegram (nested dict, per-subfield via path) ---
function tgPath(k: string): string[] { return [SEC, "telegram", k]; }
function tgHint(k: string): string { const v = inheritedForPath(payload, prof, tgPath(k)); return v == null ? "(nic)" : String(v); }
function tgState(k: string): "inherit" | "override" { return overrideValuePath(payload, prof, tgPath(k)) === undefined ? "inherit" : "override"; }
function tgValue(k: string): string { const v = overrideValuePath(payload, prof, tgPath(k)); return v === undefined ? String(inheritedForPath(payload, prof, tgPath(k)) ?? "") : String(v); }
function setTgState(k: string, s: "inherit" | "override"): void {
  payload = { ...payload, profiles: s === "inherit" ? clearOverridePath(payload.profiles, prof, tgPath(k)) : setOverridePath(payload.profiles, prof, tgPath(k), String(inheritedForPath(payload, prof, tgPath(k)) ?? "")) };
  onChange();
}
function setTg(k: string, v: string): void { payload = { ...payload, profiles: setOverridePath(payload.profiles, prof, tgPath(k), v) }; onChange(); }
```

Keep the existing base-mode `const`s (`enabled`/`sound`/`on`/`onState`/`backends`/`backendsState`/`telegram`) and functions (`set`/`setTri`/`setTelegram`/`setSecret`/`clearSecret`) UNCHANGED — `setSecret`/`clearSecret` are reused by overlay too.

- [ ] **Step 2: Restructure the template into base/overlay branches**

Replace the markup body (the `<BooleanField>`…`</fieldset>` block, NOT the `<h2>` or `<style>`) so the existing widgets live in `{:else}` verbatim and the overlay branch is added:

```svelte
<h2>Notifications{#if overlay} · overlay: {editProfile}{/if}</h2>
{#if overlay}
  <OverrideField label="enabled" state={scState("enabled")} inheritedDisplay={scHint("enabled")} onstate={(s) => setScState("enabled", s)}>
    <BooleanField label="" value={scBool("enabled")} onchange={(v) => setSc("enabled", v)} />
  </OverrideField>
  <OverrideField label="sound" state={scState("sound")} inheritedDisplay={scHint("sound")} onstate={(s) => setScState("sound", s)}>
    <BooleanField label="" value={scBool("sound")} onchange={(v) => setSc("sound", v)} />
  </OverrideField>
  <TriStateListField label="on" state={overrideState(payload, prof, SEC, "on")} list={ovList("on")} inheritLabel="Zdědit" inheritHint={`zděděno: ${listHint("on")}`} onchange={(s, l) => setOvList("on", s, l)} />
  <TriStateListField label="backends" state={overrideState(payload, prof, SEC, "backends")} list={ovList("backends")} inheritLabel="Zdědit" inheritHint={`zděděno: ${listHint("backends")}`} onchange={(s, l) => setOvList("backends", s, l)} />
  <fieldset class="tg">
    <legend>Telegram</legend>
    <OverrideField label="token" state={tgState("token_env")} inheritedDisplay={tgHint("token_env")} onstate={(s) => setTgState("token_env", s)}>
      <TokenSecretField label="" value={tgValue("token_env")} flag={secretFlag(payload, tgValue("token_env"))} oninput={(v) => setTg("token_env", v)} onset={(val) => setSecret(tgValue("token_env"), val)} onclear={() => clearSecret(tgValue("token_env"))} />
    </OverrideField>
    <OverrideField label="chat_id" state={tgState("chat_id")} inheritedDisplay={tgHint("chat_id")} onstate={(s) => setTgState("chat_id", s)}>
      <TextField label="" value={tgValue("chat_id")} oninput={(v) => setTg("chat_id", v)} />
    </OverrideField>
  </fieldset>
{:else}
  <BooleanField label="enabled" value={enabled} onchange={(v) => set("enabled", v)} />
  <BooleanField label="sound" value={sound} onchange={(v) => set("sound", v)} />
  <TriStateListField label="on" state={onState} list={on} onchange={(s, l) => setTri("on", s, l)} />
  <TriStateListField label="backends" state={backendsState} list={backends} onchange={(s, l) => setTri("backends", s, l)} />
  <fieldset class="tg">
    <legend>Telegram</legend>
    <TokenSecretField label="token" value={telegram.token_env} flag={secretFlag(payload, telegram.token_env)} oninput={(v) => setTelegram("token_env", v)} onset={(val) => setSecret(telegram.token_env, val)} onclear={() => clearSecret(telegram.token_env)} />
    <TextField label="chat_id" value={telegram.chat_id} oninput={(v) => setTelegram("chat_id", v)} />
  </fieldset>
{/if}
```

- [ ] **Step 3: Verify build + smoke**

Run: `cd desktop && npm run build && npx vitest run src/lib/fields/widgets.smoke.test.ts`
Expected: build exit 0; smoke PASS.

- [ ] **Step 4: Commit**

```bash
git add desktop/src/lib/sections/NotificationsSection.svelte
git commit -m "feat: NotificationsSection overlay-aware (scalars + lists + telegram + secret) (β2 Task 3)"
```

---

### Task 4: MacrosSection overlay-aware (whole-list override)

**Files:**
- Modify: `desktop/src/lib/sections/MacrosSection.svelte`

**Interfaces:**
- Consumes: Task 1 `macroRecords`, `overrideStatePath`; β1 `inheritedForPath`/`overrideValuePath`/`setOverridePath`/`clearOverridePath`; `OverrideField`. Adds `editProfile?: string | null` prop.
- Note: `macros` is a LIST section (backend replaces wholesale) → overlay = whole-list override, NOT per-entry.

- [ ] **Step 1: Rewrite the section**

Replace the whole file with:

```svelte
<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import OverrideField from "../fields/OverrideField.svelte";
  import {
    macrosOf, addMacro, removeMacro, updateMacro, macroRecords,
    inheritedForPath, overrideValuePath, overrideStatePath, setOverridePath, clearOverridePath,
    type ConfigPayload, type MacroRecord,
  } from "../configClient";

  let { payload = $bindable(), onChange, editProfile = null }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; editProfile?: string | null } = $props();

  const overlay = $derived(editProfile != null && editProfile !== "default");
  const prof = $derived(editProfile ?? "");

  // --- base mode (unchanged) ---
  const macros = $derived(macrosOf(payload));
  function set(i: number, field: keyof MacroRecord, v: string): void { payload = updateMacro(payload, i, field, v); onChange(); }
  function add(): void { payload = addMacro(payload); onChange(); }
  function remove(i: number): void { payload = removeMacro(payload, i); onChange(); }

  // --- overlay mode: whole-list override (macros replace wholesale in the backend merge) ---
  function ovMacros(): MacroRecord[] { return macroRecords(overrideValuePath(payload, prof, ["macros"])); }
  function inhMacros(): MacroRecord[] { return macroRecords(inheritedForPath(payload, prof, ["macros"])); }
  function ovState(): "inherit" | "override" { return overrideStatePath(payload, prof, ["macros"]) === "default" ? "inherit" : "override"; }
  function writeOv(list: MacroRecord[]): void { payload = { ...payload, profiles: setOverridePath(payload.profiles, prof, ["macros"], list) }; onChange(); }
  function setOvState(s: "inherit" | "override"): void {
    payload = { ...payload, profiles: s === "inherit" ? clearOverridePath(payload.profiles, prof, ["macros"]) : setOverridePath(payload.profiles, prof, ["macros"], inhMacros()) };
    onChange();
  }
  function ovSet(i: number, field: keyof MacroRecord, v: string): void { writeOv(ovMacros().map((m, j) => (j === i ? { ...m, [field]: v } : m))); }
  function ovAdd(): void { writeOv([...ovMacros(), { label: "", text: "" }]); }
  function ovRemove(i: number): void { writeOv(ovMacros().filter((_, j) => j !== i)); }
</script>

<h2>Macros{#if overlay} · overlay: {editProfile}{/if}</h2>
{#if overlay}
  <OverrideField label="macros" state={ovState()} inheritedDisplay={`${inhMacros().length} maker`} onstate={setOvState}>
    {#each ovMacros() as m, i (i)}
      <fieldset>
        <legend>{m.label || "(nové makro)"} <button type="button" onclick={() => ovRemove(i)}>×</button></legend>
        <TextField label="label" value={m.label} oninput={(v) => ovSet(i, "label", v)} />
        <TextField label="text" value={m.text} oninput={(v) => ovSet(i, "text", v)} />
      </fieldset>
    {/each}
    <button type="button" onclick={ovAdd}>+ přidat makro</button>
  </OverrideField>
{:else}
  <!-- Index keying: append/remove list, no reordering, no per-row transient state. Same
       rationale as ServersSection — a stable-id apparatus would add needless complexity. -->
  {#each macros as m, i (i)}
    <fieldset>
      <legend>{m.label || "(nové makro)"} <button type="button" onclick={() => remove(i)}>×</button></legend>
      <TextField label="label" value={m.label} oninput={(v) => set(i, "label", v)} />
      <TextField label="text" value={m.text} oninput={(v) => set(i, "text", v)} />
    </fieldset>
  {/each}
  <button type="button" onclick={add}>+ přidat makro</button>
{/if}

<style>
  h2 { margin: 0 0 8px; }
  fieldset { border: 1px solid #2a2a30; border-radius: 6px; margin: 8px 0; padding: 8px 12px; }
  legend { color: #ccc; } legend button { color: #e05050; background: none; border: 0; cursor: pointer; }
  button { background: #1b1b1f; border: 1px solid #2a2a30; color: inherit; border-radius: 4px; padding: 4px 8px; cursor: pointer; }
</style>
```

- [ ] **Step 2: Verify build + smoke**

Run: `cd desktop && npm run build && npx vitest run src/lib/fields/widgets.smoke.test.ts`
Expected: build exit 0; smoke PASS.

- [ ] **Step 3: Commit**

```bash
git add desktop/src/lib/sections/MacrosSection.svelte
git commit -m "feat: MacrosSection overlay-aware (whole-list override) (β2 Task 4)"
```

---

### Task 5: StartProfilesSection overlay per-entry + base map-empty

**Files:**
- Modify: `desktop/src/lib/sections/StartProfilesSection.svelte`

**Interfaces:**
- Consumes: Task 1 `inheritedSection`; Task 2 `mapSectionState`/`setMapSectionState`; β1 `overrideValuePath`/`setOverridePath`/`clearOverridePath`; existing `startProfileRows`/`serializeNamedRows`/`applyMapSection`; `OverrideField`, `ListField`, `TextField`. Adds `editProfile?: string | null` prop. Keeps `reloadRev`.

- [ ] **Step 1: Rewrite the section**

Replace the whole file with:

```svelte
<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import ListField from "../fields/ListField.svelte";
  import OverrideField from "../fields/OverrideField.svelte";
  import {
    startProfileRows, serializeNamedRows, applyMapSection,
    mapSectionState, setMapSectionState, inheritedSection,
    overrideValuePath, setOverridePath, clearOverridePath,
    type ConfigPayload, type StartProfileRow, type ListFieldState,
  } from "../configClient";

  let { payload = $bindable(), onChange, onError, reloadRev, editProfile = null }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; reloadRev: number; editProfile?: string | null } = $props();

  const SEC = "start_profiles";
  const overlay = $derived(editProfile != null && editProfile !== "default");
  const prof = $derived(editProfile ?? "");
  const argvOf = (v: unknown): string[] => (Array.isArray(v) ? v.map(String) : []);

  // --- base mode: local rows (re-seed only on reloadRev) + explicit-empty mode ---
  let rows = $state<StartProfileRow[]>(startProfileRows(payload));
  let seenRev = $state(reloadRev);
  let mode = $state<ListFieldState>(mapSectionState(payload, SEC));

  $effect(() => {
    if (reloadRev !== seenRev) {
      seenRev = reloadRev;
      rows = startProfileRows(payload);
      mode = mapSectionState(payload, SEC);
    }
  });

  function commit(next: StartProfileRow[]): void {
    rows = next;
    const { duplicate, section } = serializeNamedRows(next, (r) => r.argv);
    if (duplicate) { onError("duplicitní jméno start profilu — neuloží se, dokud nepřejmenuješ"); return; }
    const updated = applyMapSection(payload, SEC, section);
    if (updated === null) return;
    payload = updated;
    onChange();
  }
  function setMode(m: ListFieldState): void {
    mode = m;
    if (m === "custom") { commit(rows); return; } // reveal editor; rows drive the map
    payload = setMapSectionState(payload, SEC, m);
    onChange();
  }
  function rename(i: number, name: string): void { commit(rows.map((r, j) => (j === i ? { ...r, name } : r))); }
  function setArgv(i: number, argv: string[]): void { commit(rows.map((r, j) => (j === i ? { ...r, argv } : r))); }
  function add(): void { commit([...rows, { name: "", argv: [] }]); }
  function remove(i: number): void { commit(rows.filter((_, j) => j !== i)); }

  // --- overlay mode: per-entry override (read live, no local rows) ---
  function inhMap(): Record<string, unknown> { return inheritedSection(payload, prof, SEC); }
  function ownMap(): Record<string, unknown> { const v = overrideValuePath(payload, prof, [SEC]); return v != null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {}; }
  function entryNames(): string[] { return Array.from(new Set([...Object.keys(inhMap()), ...Object.keys(ownMap())])); }
  function isInherited(name: string): boolean { return name in inhMap(); }
  function entryState(name: string): "inherit" | "override" { return name in ownMap() ? "override" : "inherit"; }
  function inhArgv(name: string): string[] { return argvOf(inhMap()[name]); }
  function ovArgv(name: string): string[] { return argvOf(ownMap()[name]); }
  function setEntryState(name: string, s: "inherit" | "override"): void {
    payload = { ...payload, profiles: s === "inherit" ? clearOverridePath(payload.profiles, prof, [SEC, name]) : setOverridePath(payload.profiles, prof, [SEC, name], inhArgv(name)) };
    onChange();
  }
  function setEntryArgv(name: string, argv: string[]): void { payload = { ...payload, profiles: setOverridePath(payload.profiles, prof, [SEC, name], argv) }; onChange(); }
  let newName = $state("");
  function addEntry(): void {
    const n = newName.trim();
    if (n === "") return;
    if (entryNames().includes(n)) { onError(`položka '${n}' už existuje`); return; }
    payload = { ...payload, profiles: setOverridePath(payload.profiles, prof, [SEC, n], []) };
    newName = "";
    onChange();
  }
  function removeOwn(name: string): void { payload = { ...payload, profiles: clearOverridePath(payload.profiles, prof, [SEC, name]) }; onChange(); }
</script>

<h2>Start profiles{#if overlay} · overlay: {editProfile}{/if}</h2>
{#if overlay}
  <p class="hint">Per-entry overlay: přepiš zděděnou položku nebo přidej profilovou. Zděděné položky nelze v overlay smazat (backend merge je aditivní).</p>
  {#each entryNames() as name (name)}
    <fieldset>
      <legend>{name}{#if !isInherited(name)} <button type="button" onclick={() => removeOwn(name)}>×</button>{/if}</legend>
      <OverrideField label="argv" state={entryState(name)} inheritedDisplay={inhArgv(name).join(" · ") || "(prázdné)"} onstate={(s) => setEntryState(name, s)}>
        <ListField label="" value={ovArgv(name)} onchange={(v) => setEntryArgv(name, v)} />
      </OverrideField>
    </fieldset>
  {/each}
  <div class="create">
    <input placeholder="jméno profilové položky" bind:value={newName} />
    <button type="button" onclick={addEntry}>+ přidat (jen profil)</button>
  </div>
{:else}
  <p class="hint">Spouštěcí příkaz (argv) pro každý typ agenta startovaného z decku.</p>
  <div class="modes">
    <button type="button" class:active={mode === "default"} onclick={() => setMode("default")}>Výchozí</button>
    <button type="button" class:active={mode === "custom"} onclick={() => setMode("custom")}>Vlastní</button>
    <button type="button" class:active={mode === "empty"} onclick={() => setMode("empty")}>Vypnuto</button>
  </div>
  {#if mode === "empty"}
    <p class="hint">Žádné launchery (explicitní prázdná mapa).</p>
  {:else if mode === "custom"}
    {#each rows as e, i (i)}
      <fieldset>
        <legend>{e.name || "(nový profil)"} <button type="button" onclick={() => remove(i)}>×</button></legend>
        <TextField label="name" value={e.name} oninput={(v) => rename(i, v)} />
        {#if e.name.trim() !== ""}
          <ListField label="argv" value={e.argv} onchange={(v) => setArgv(i, v)} />
        {:else}
          <p class="hint">Zadej jméno profilu pro úpravu argv.</p>
        {/if}
      </fieldset>
    {/each}
    <button type="button" onclick={add}>+ přidat profil</button>
  {:else}
    <p class="hint">Výchozí launchery (DEFAULT_START_PROFILES). Přepni na „Vlastní" pro úpravu.</p>
  {/if}
{/if}

<style>
  h2 { margin: 0 0 8px; }
  .hint { color: #888; margin: 0 0 8px; }
  .modes { display: flex; gap: 4px; margin: 8px 0; }
  .modes button { background: #1b1b1f; border: 1px solid #2a2a30; color: inherit; border-radius: 4px; padding: 4px 10px; cursor: pointer; }
  .modes button.active { background: #2d3550; border-color: #4a5a80; }
  .create { display: flex; gap: 6px; margin: 8px 0; }
  .create input { flex: 1; background: #141417; border: 1px solid #2a2a30; color: inherit; padding: 4px 6px; border-radius: 4px; }
  fieldset { border: 1px solid #2a2a30; border-radius: 6px; margin: 8px 0; padding: 8px 12px; }
  legend { color: #ccc; } legend button { color: #e05050; background: none; border: 0; cursor: pointer; }
  button { background: #1b1b1f; border: 1px solid #2a2a30; color: inherit; border-radius: 4px; padding: 4px 8px; cursor: pointer; }
</style>
```

- [ ] **Step 2: Verify build + smoke**

Run: `cd desktop && npm run build && npx vitest run src/lib/fields/widgets.smoke.test.ts`
Expected: build exit 0; smoke PASS.

- [ ] **Step 3: Commit**

```bash
git add desktop/src/lib/sections/StartProfilesSection.svelte
git commit -m "feat: StartProfilesSection overlay per-entry + base map-empty (β2 Task 5)"
```

---

### Task 6: AnswerProfilesSection overlay per-entry

**Files:**
- Modify: `desktop/src/lib/sections/AnswerProfilesSection.svelte`

**Interfaces:**
- Consumes: Task 1 `inheritedSection`; β1 `overrideValuePath`/`setOverridePath`/`clearOverridePath`; `OverrideField`, `ListField`, `TriStateListField`. Adds `editProfile?: string | null` prop. Keeps `reloadRev` for base mode.
- Per-entry override unit = the whole named entry dict `{approve, deny, stop, approve_always}`.

- [ ] **Step 1: Add overlay imports + props + helpers**

Extend the configClient import with `inheritedSection, overrideValuePath, setOverridePath, clearOverridePath` and add `OverrideField` import. Change props + add helpers:

```ts
import OverrideField from "../fields/OverrideField.svelte";
// ... existing imports + the four above ...

let { payload = $bindable(), onChange, onError, reloadRev, editProfile = null }:
  { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; reloadRev: number; editProfile?: string | null } = $props();

const SEC = "answer_profiles";
const overlay = $derived(editProfile != null && editProfile !== "default");
const prof = $derived(editProfile ?? "");
const argvOf = (v: unknown): string[] => (Array.isArray(v) ? v.map(String) : []);
const dictOf = (v: unknown): Record<string, unknown> => (v != null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {});

// --- overlay mode: per-entry override (whole entry dict) ---
function inhMap(): Record<string, unknown> { return inheritedSection(payload, prof, SEC); }
function ownMap(): Record<string, unknown> { const v = overrideValuePath(payload, prof, [SEC]); return dictOf(v); }
function entryNames(): string[] { return Array.from(new Set([...Object.keys(inhMap()), ...Object.keys(ownMap())])); }
function isInherited(name: string): boolean { return name in inhMap(); }
function entryState(name: string): "inherit" | "override" { return name in ownMap() ? "override" : "inherit"; }
function inhEntry(name: string): Record<string, unknown> { return dictOf(inhMap()[name]); }
function ovEntry(name: string): Record<string, unknown> { return dictOf(ownMap()[name]); }
function inhSummary(name: string): string {
  const e = inhEntry(name);
  return LIST_KEYS.map((k) => `${k}:${argvOf(e[k]).length}`).join(" · ");
}
function writeEntry(name: string, entry: Record<string, unknown>): void { payload = { ...payload, profiles: setOverridePath(payload.profiles, prof, [SEC, name], entry) }; onChange(); }
function setEntryState(name: string, s: "inherit" | "override"): void {
  payload = { ...payload, profiles: s === "inherit" ? clearOverridePath(payload.profiles, prof, [SEC, name]) : setOverridePath(payload.profiles, prof, [SEC, name], inhEntry(name)) };
  onChange();
}
function setEntryKey(name: string, key: (typeof LIST_KEYS)[number], v: string[]): void { writeEntry(name, { ...ovEntry(name), [key]: v }); }
function aaStateOv(name: string): ListFieldState { const e = ovEntry(name); if (!("approve_always" in e)) return "default"; const v = e.approve_always; return Array.isArray(v) && v.length === 0 ? "empty" : "custom"; }
function setAAOv(name: string, state: ListFieldState, list: string[]): void {
  const e = { ...ovEntry(name) };
  if (state === "default") delete e.approve_always;
  else e.approve_always = state === "empty" ? [] : list;
  writeEntry(name, e);
}
let newName = $state("");
function addEntry(): void {
  const n = newName.trim();
  if (n === "") return;
  if (entryNames().includes(n)) { onError(`položka '${n}' už existuje`); return; }
  payload = { ...payload, profiles: setOverridePath(payload.profiles, prof, [SEC, n], { approve: [], deny: [], stop: [] }) };
  newName = "";
  onChange();
}
function removeOwn(name: string): void { payload = { ...payload, profiles: clearOverridePath(payload.profiles, prof, [SEC, name]) }; onChange(); }
```

Keep the existing base-mode `rows`/`seenRev`/`$effect`/`commit`/`rename`/`setList`/`aaState`/`setApproveAlways`/`add`/`remove` UNCHANGED.

- [ ] **Step 2: Restructure the template into base/overlay branches**

Wrap the existing `{#each rows ...}` editor + add-button in `{:else}` and add the overlay branch:

```svelte
<h2>Answer profiles{#if overlay} · overlay: {editProfile}{/if}</h2>
{#if overlay}
  <p class="hint">Per-entry overlay: přepiš zděděný answer profil nebo přidej profilový. Zděděné položky nelze v overlay smazat.</p>
  {#each entryNames() as name (name)}
    <fieldset>
      <legend>{name}{#if !isInherited(name)} <button type="button" onclick={() => removeOwn(name)}>×</button>{/if}</legend>
      <OverrideField label="keys" state={entryState(name)} inheritedDisplay={inhSummary(name)} onstate={(s) => setEntryState(name, s)}>
        {#each LIST_KEYS as k}
          <ListField label={k} value={argvOf(ovEntry(name)[k])} onchange={(v) => setEntryKey(name, k, v)} />
        {/each}
        <TriStateListField label="approve_always" state={aaStateOv(name)} list={argvOf(ovEntry(name).approve_always)} onchange={(s, l) => setAAOv(name, s, l)} />
      </OverrideField>
    </fieldset>
  {/each}
  <div class="create">
    <input placeholder="jméno profilové položky" bind:value={newName} />
    <button type="button" onclick={addEntry}>+ přidat (jen profil)</button>
  </div>
{:else}
  <p class="hint">Klávesy posílané agentovi pro approve / deny / stop podle typu agenta.</p>
  {#each rows as e, i (i)}
    <fieldset>
      <legend>{e.name || "(nový profil)"} <button type="button" onclick={() => remove(i)}>×</button></legend>
      <TextField label="name" value={e.name} oninput={(v) => rename(i, v)} />
      {#if e.name.trim() !== ""}
        {#each LIST_KEYS as k}
          <ListField label={k} value={e[k] ?? []} onchange={(v) => setList(i, k, v)} />
        {/each}
        <TriStateListField label="approve_always" state={aaState(e)} list={e.approve_always ?? []} onchange={(s, l) => setApproveAlways(i, s, l)} />
      {:else}
        <p class="hint">Zadej jméno profilu pro úpravu kláves.</p>
      {/if}
    </fieldset>
  {/each}
  <button type="button" onclick={add}>+ přidat profil</button>
{/if}
```

Add a `.create` rule to `<style>` if not present (mirror StartProfilesSection):

```css
.create { display: flex; gap: 6px; margin: 8px 0; }
.create input { flex: 1; background: #141417; border: 1px solid #2a2a30; color: inherit; padding: 4px 6px; border-radius: 4px; }
```

- [ ] **Step 3: Verify build + smoke**

Run: `cd desktop && npm run build && npx vitest run src/lib/fields/widgets.smoke.test.ts`
Expected: build exit 0; smoke PASS.

- [ ] **Step 4: Commit**

```bash
git add desktop/src/lib/sections/AnswerProfilesSection.svelte
git commit -m "feat: AnswerProfilesSection overlay per-entry (β2 Task 6)"
```

---

### Task 7: ProfilesSection — servers serverless (`[]`) authoring

**Files:**
- Modify: `desktop/src/lib/sections/ProfilesSection.svelte`

**Interfaces:**
- Consumes: Task 2 `profileServersState`/`setProfileServersExplicit`/`clearProfileServers`; `OverrideField`. (Section keeps NO `editProfile` prop — it is a meta-section editing per-profile directly.)

- [ ] **Step 1: Swap server writes to explicit + add inherit/explicit toggle**

In the `<script>`, extend the configClient import with `profileServersState, setProfileServersExplicit, clearProfileServers` and add `OverrideField`:

```ts
import OverrideField from "../fields/OverrideField.svelte";
```

Replace `toggleServer` and add the state helpers:

```ts
function srvState(name: string): "inherit" | "override" { return profileServersState(payload, name) === "explicit" ? "override" : "inherit"; }
function setSrvState(name: string, s: "inherit" | "override"): void {
  payload = s === "inherit" ? clearProfileServers(payload, name) : setProfileServersExplicit(payload, name, profileServers(payload, name));
  onChange();
}
function toggleServer(name: string, id: string, on: boolean): void {
  const cur = profileServers(payload, name);
  const next = on ? [...cur, id] : cur.filter((s) => s !== id);
  payload = setProfileServersExplicit(payload, name, next); // explicit: keeps [] (serverless)
  onChange();
}
```

(`setProfileServers` is no longer used here but stays in configClient for back-compat.)

- [ ] **Step 2: Wrap the servers block in OverrideField**

Replace the `<div class="servers">…</div>` block with:

```svelte
<OverrideField label="servers" state={srvState(name)} inheritedDisplay="zdědí base servery" onstate={(s) => setSrvState(name, s)}>
  <div class="servers">
    {#if serverOptions(name).length === 0}
      <span class="hint">žádné servery v bázi — přidej je v sekci Servers</span>
    {:else}
      {#each serverOptions(name) as opt (opt.id)}
        <label class="chk">
          <input
            type="checkbox"
            checked={profileServers(payload, name).includes(opt.id)}
            onchange={(e) => toggleServer(name, opt.id, (e.target as HTMLInputElement).checked)}
          />
          {opt.id}{#if !opt.known} <span class="unknown">(neznámý)</span>{/if}
        </label>
      {/each}
      {#if profileServers(payload, name).length === 0}
        <span class="hint">serverless: profil poběží bez serverů (explicitní prázdný výběr)</span>
      {/if}
    {/if}
  </div>
</OverrideField>
```

- [ ] **Step 3: Verify build + smoke**

Run: `cd desktop && npm run build && npx vitest run src/lib/fields/widgets.smoke.test.ts`
Expected: build exit 0; smoke PASS.

- [ ] **Step 4: Commit**

```bash
git add desktop/src/lib/sections/ProfilesSection.svelte
git commit -m "feat: ProfilesSection serverless (explicit []) servers authoring (β2 Task 7)"
```

---

### Task 8: ConfigApp — wire editProfile to remaining sections + remove base-only note

**Files:**
- Modify: `desktop/src/ConfigApp.svelte`

**Interfaces:**
- Consumes: Tasks 3–6 sections now accept `editProfile`. After β2 all `_OVERLAY_SECTIONS` are overlay-aware → `BASE_ONLY_IN_OVERLAY` becomes empty and the note is removed.

- [ ] **Step 1: Remove the base-only note constant + markup**

Delete the `BASE_ONLY_IN_OVERLAY` const (lines ~61-65) and its block comment, and remove the note `<p class="overlaynote">…</p>` (the `{#if editProfile && BASE_ONLY_IN_OVERLAY.includes(active)}` block, ~213-215). The `.overlaynote` CSS rule may be left (harmless) or removed.

- [ ] **Step 2: Pass `{editProfile}` to the four newly overlay-aware sections**

Update the section dispatch lines so Macros / Start profiles / Notifications / Answer profiles receive `{editProfile}` (Start/Answer keep `{reloadRev}`):

```svelte
{:else if active === "Macros"}
  <MacrosSection bind:payload {editProfile} onChange={markDirty} onError={(m) => setBanner("error", m)} />
{:else if active === "Start profiles"}
  <StartProfilesSection bind:payload {editProfile} {reloadRev} onChange={markDirty} onError={(m) => setBanner("error", m)} />
{:else if active === "Notifications"}
  <NotificationsSection bind:payload {editProfile} onChange={markDirty} onError={(m) => setBanner("error", m)} />
{:else if active === "Safety"}
  <SafetySection bind:payload {editProfile} onChange={markDirty} onError={(m) => setBanner("error", m)} />
{:else if active === "Answer profiles"}
  <AnswerProfilesSection bind:payload {editProfile} {reloadRev} onChange={markDirty} onError={(m) => setBanner("error", m)} />
```

(Servers + Profiles dispatch lines stay unchanged — no overlay.)

- [ ] **Step 3: Verify build + full suite**

Run: `cd desktop && npm run build && npx vitest run`
Expected: build exit 0; all Vitest pass (no regression vs β1 baseline 104 + new β2 tests).

- [ ] **Step 4: Commit**

```bash
git add desktop/src/ConfigApp.svelte
git commit -m "feat: ConfigApp wires editProfile to all overlay sections + drops base-only note (β2 Task 8)"
```

---

## Self-Review

**Spec coverage:** Notifications overlay (Task 3) · Macros whole-list overlay (Task 4) · Start per-entry + base map-empty (Task 5) · Answer per-entry (Task 6) · serverless `profile.servers=[]` (Task 7) · resolver + helpers (Tasks 1-2) · ConfigApp wiring + note removal (Task 8). klik-to-jump = explicit non-goal (deferred). All spec sections map to a task.

**Type consistency:** `inheritedSection`/`mergeSection`/`overrideStatePath`/`macroRecords` (Task 1) consumed by Tasks 4-6 with matching signatures. `mapSectionState`/`setMapSectionState` (Task 2) consumed by Task 5; `profileServersState`/`setProfileServersExplicit`/`clearProfileServers` (Task 2) by Task 7. `OverrideField` props `{label, state, inheritedDisplay, onstate, children}` and overlay `TriStateListField` props `{state, list, inheritLabel, inheritHint, onchange}` match β1's established API. `editProfile?: string | null` default null preserves base render (every section green standalone before Task 8 wires it).

**No placeholders:** every step carries the actual code/command/expected output.
