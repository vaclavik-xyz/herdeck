# Config editor frontend — řez 4b-i (Profiles + switcher + deferred) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make named profiles manageable in the config editor — a working **Profiles** section (create/delete + per-profile `extends` and `servers`), a functional **active-profile switcher** (persist via `set_active`, env-locked + dirty guards), a small structured **Banner** for messages, an **Apply-time orphaned-keychain-secret cleanup**, and the two řez-3 **deferred fixes** (`token_env` keychain orphan; `DELETE /secret/{env}` percent-encode) — building on řez 4a (merged `6893a03`).

**Architecture:** Thin GUI over the existing backend API. Profile CRUD and `extends`/`servers` are edits to the in-memory `payload.profiles[name]` persisted by the existing global **Apply** (`POST /config`); switching the active profile is the existing immediate `config_set_active` (`POST /profiles/active`). Pure logic lives in unit-tested `configClient.ts` helpers; sections/shell are thin Svelte verified by the build gate. No new Tauri commands, no new HTTP routes. Per-section profile **overlay** editing, the three-state `OverrideField`, explicit-empty authoring, and klik-to-jump are **out of scope** (řez 4b-ii).

**Tech Stack:** Svelte 5 runes (`$state`/`$derived`/`$props`/`$bindable`), TypeScript, Vitest (pure-logic tests), Rust (Tauri proxy — one helper), Python 3.12+ (one sidecar one-liner), ruff + pytest, cargo.

## Global Constraints

Copied from `docs/superpowers/specs/2026-06-26-config-editor-frontend-4b-i-design.md` and the established řez-3/4a patterns. Every task implicitly includes these.

- **No new config logic in the frontend.** `configClient.ts` only maps the `read()` payload to/from editor structures; validation stays in the backend (`POST /config[/validate]`). No config resolution/merging/cycle-checking reimplemented in JS.
- **The sidecar token NEVER lives in JS.** Every backend call goes through the existing token-free Tauri commands (`config_read`/`validate`/`write`/`set_active`/`secret_set`/`secret_clear`); the Rust shell injects the token. No `fetch()` to the sidecar. Řez 4b-i adds NO new Tauri commands and NO new HTTP routes.
- **Secret VALUES are one-way** — never in the model, TOML, response body, or logs. Secrets carry only `{set, source}` presence flags. The orphan-cleanup CLEARS a keychain entry (`config_secret_clear`); it never reads a value back and never migrates one.
- **Save model = explicit global Apply.** Profile CRUD + `extends`/`servers` mutate the in-memory `payload` (`$bindable`) and mark dirty; nothing persists until **Apply** POSTs the whole `{base, profiles, local}`. `set_active` and secret set/clear are immediate side-effects OUTSIDE Apply (existing řez-3 behavior).
- **Switcher = ACTIVE-profile selector, not an editing switch.** Selecting a profile calls `config_set_active` (persists `local.toml` `active_profile` + backend reload). It does NOT change which config the section forms edit — forms stay base-only in 4b-i (per-section overlay editing is řez 4b-ii). The switcher is **disabled when `payload.envLocked`** (env lock) OR **when `dirty`** (a `set_active` reload would clobber unsaved edits).
- **Absent ≠ empty (the defaults rule).** A profile's absent `servers` key means "inherit base servers"; an explicit `[]` means a serverless profile (řez 4b-ii). So an emptied `servers` selection OMITS the key (returns to inherit), never writes `[]`. `extends` is a scalar (writing `"default"` equals the default), so it is exempt.
- **Pure logic = TDD (Vitest); Svelte components = build gate.** Pure functions in `configClient.ts` follow strict TDD (failing test first). Svelte components (`ProfilesSection`, `Banner`, the `ConfigApp` shell) are verified by `npm run build` (vite compiles the Svelte + TS). NO `svelte-check` dependency exists — do not add it. (`ProfilesSection`/`Banner` are imported by `ConfigApp`, so `vite build` reaches them — no compile-smoke needed.)
- **Backend payload shapes are used verbatim.** `read()` returns `{base, profiles, local, secrets, env_locked, active_profile}` (parsed to `ConfigPayload` with `envLocked`/`activeProfile`). `config_set_active` returns `{changed: bool}` (or a 400 surfaced as a thrown error). `config_secret_clear` returns the HTTP status (204 ok).
- **CI parity before any push:** `ruff check src tests` AND `python -m pytest` for Python changes; `cargo test` (in `desktop/src-tauri`) for Rust changes; `npm run build` + `npx vitest run` for desktop changes. (Pushing is a separate, user-approved step; this is the per-task gate.)

### Environment facts (override generic commands)

- Python test runner is the project venv (python3.14): `.venv/bin/python -m pytest <args>`. Do NOT run bare `python -m pytest` — an `rtk` wrapper intercepts it and fails. ruff: `.venv/bin/ruff check src tests` (CI checks BOTH `src` and `tests`).
- Desktop commands run from `desktop/`: `cd desktop && npx vitest run <path>` / `npm run build`. `node_modules` is installed; do not reinstall. No svelte-check.
- Cargo is NOT on `PATH`; it lives at `~/.cargo/bin`. Rust tests: `cd desktop/src-tauri && ~/.cargo/bin/cargo test`.
- Baseline at branch start (merge `6893a03`): Python 573 pass + ruff clean; desktop 71 vitest + build exit 0.

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `desktop/src/lib/configClient.ts` | modify | new pure helpers: `profileNames`, `createProfile`, `deleteProfile`, `profileExtends`, `setProfileExtends`, `profileServers`, `setProfileServers`, `referencedTokenEnvs`, `orphanedSecrets`, `parseActiveChanged` |
| `desktop/src/lib/configClient.test.ts` | modify | Vitest for all the above |
| `desktop/src/lib/sections/ProfilesSection.svelte` | create | Profiles UI: list + create/delete + `extends` (SelectField) + `servers` (checkbox list of base server ids) |
| `desktop/src/lib/Banner.svelte` | create | small message banner with `kind` + optional action button |
| `desktop/src/ConfigApp.svelte` | modify | wire Profiles branch; `notice` string → `banner` state via `Banner`; functional switcher (env-lock + dirty guard); Apply-time orphan cleanup |
| `desktop/src-tauri/src/http.rs` | modify | `percent_encode_segment` helper (+ unit test) |
| `desktop/src-tauri/src/lib.rs` | modify | `config_secret_clear` percent-encodes the `token_env` path segment |
| `src/herdeck/deckapp/server.py` | modify | `do_DELETE` `unquote`s the secret path segment |
| `tests/test_deckapp.py` | modify | route test: percent-encoded `token_env` is decoded before `clear_secret` |

---

### Task 1: configClient — profile CRUD helpers

**Files:**
- Modify: `desktop/src/lib/configClient.ts` (append after `parseActiveChanged`'s neighbors — i.e. after the existing `putList`, before `commandTransport`)
- Test: `desktop/src/lib/configClient.test.ts` (append a `describe`)

**Interfaces:**
- Consumes: `ConfigPayload`, the private `clone`/`asDict` (already in file).
- Produces:
  - `ProfileResult = { ok: true; payload: ConfigPayload } | { ok: false; error: string }`
  - `profileNames(payload): string[]` — `Object.keys(payload.profiles)`.
  - `createProfile(payload, name): ProfileResult` — trims `name`; errors on blank, the reserved `"default"`, or a duplicate; else a NEW payload with `profiles[name] = {}`.
  - `deleteProfile(payload, name): ConfigPayload` — NEW payload with `profiles[name]` removed; if `local.active_profile === name`, that now-dangling selection is also dropped from `local` (so the next Apply doesn't write an unknown active profile). Input untouched.

- [ ] **Step 1: Write the failing tests**

Append to `desktop/src/lib/configClient.test.ts`:

```typescript
import { profileNames, createProfile, deleteProfile } from "./configClient";

describe("profile CRUD", () => {
  it("profileNames lists the named profiles", () => {
    const p = parseConfig({ profiles: { mobile: {}, work: {} } })!;
    expect(profileNames(p).sort()).toEqual(["mobile", "work"]);
    expect(profileNames(parseConfig({})!)).toEqual([]);
  });

  it("createProfile adds an empty profile without touching the input", () => {
    const p = parseConfig({ profiles: { mobile: {} } })!;
    const res = createProfile(p, "work");
    expect(res.ok).toBe(true);
    if (res.ok) {
      expect(profileNames(res.payload).sort()).toEqual(["mobile", "work"]);
      expect(res.payload.profiles.work).toEqual({});
    }
    expect(profileNames(p)).toEqual(["mobile"]); // input untouched
  });

  it("createProfile trims the name", () => {
    const res = createProfile(parseConfig({})!, "  work  ");
    expect(res.ok).toBe(true);
    if (res.ok) expect(profileNames(res.payload)).toEqual(["work"]);
  });

  it("createProfile rejects blank, reserved 'default', and duplicates", () => {
    const p = parseConfig({ profiles: { mobile: {} } })!;
    expect(createProfile(p, "  ")).toEqual({ ok: false, error: expect.stringContaining("prázdné") });
    expect(createProfile(p, "default")).toEqual({ ok: false, error: expect.stringContaining("default") });
    expect(createProfile(p, "mobile")).toEqual({ ok: false, error: expect.stringContaining("existuje") });
  });

  it("deleteProfile removes the profile on a copy", () => {
    const p = parseConfig({ profiles: { mobile: {}, work: {} } })!;
    const next = deleteProfile(p, "work");
    expect(profileNames(next)).toEqual(["mobile"]);
    expect(profileNames(p).sort()).toEqual(["mobile", "work"]); // input untouched
  });

  it("deleteProfile drops a now-dangling local active_profile", () => {
    const p = parseConfig({ profiles: { mobile: {} }, local: { active_profile: "mobile", hardware: { brightness: 70 } } })!;
    const next = deleteProfile(p, "mobile");
    expect("active_profile" in next.local).toBe(false); // dangling selection cleared
    expect((next.local.hardware as Record<string, unknown>).brightness).toBe(70); // rest kept
  });

  it("deleteProfile keeps an unrelated local active_profile", () => {
    const p = parseConfig({ profiles: { mobile: {}, work: {} }, local: { active_profile: "work" } })!;
    const next = deleteProfile(p, "mobile");
    expect(next.local.active_profile).toBe("work");
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd desktop && npx vitest run src/lib/configClient.test.ts`
Expected: FAIL — `profileNames`/`createProfile`/`deleteProfile` are not exported.

- [ ] **Step 3: Implement the helpers**

Append to `desktop/src/lib/configClient.ts` (after the `putList` function, before `commandTransport`):

```typescript
// --- profiles (řez 4b-i) ---

/** Result of a profile mutation that can fail validation. */
export type ProfileResult =
  | { ok: true; payload: ConfigPayload }
  | { ok: false; error: string };

/** The named profile keys (the implicit base is "default", never listed here). */
export function profileNames(payload: ConfigPayload): string[] {
  return Object.keys(payload.profiles);
}

/** NEW payload with an empty profile `name`, or an error when the trimmed name is
 *  blank, the reserved "default", or already taken. Input untouched. */
export function createProfile(payload: ConfigPayload, name: string): ProfileResult {
  const n = name.trim();
  if (n === "") return { ok: false, error: "jméno profilu nesmí být prázdné" };
  if (n === "default") return { ok: false, error: "'default' je rezervováno pro bázi" };
  if (n in payload.profiles) return { ok: false, error: `profil '${n}' už existuje` };
  const profiles = { ...clone(payload.profiles), [n]: {} };
  return { ok: true, payload: { ...payload, profiles } };
}

/** NEW payload with profile `name` removed. If `name` was the local active
 *  profile, that now-dangling selection is dropped from `local` too (so the next
 *  Apply doesn't write an unknown active profile); other local keys are kept.
 *  Input untouched. */
export function deleteProfile(payload: ConfigPayload, name: string): ConfigPayload {
  const profiles = clone(payload.profiles);
  delete profiles[name];
  let local = payload.local;
  if (asDict(payload.local).active_profile === name) {
    local = { ...clone(payload.local) };
    delete (local as Record<string, unknown>).active_profile;
  }
  return { ...payload, profiles, local };
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd desktop && npx vitest run src/lib/configClient.test.ts`
Expected: PASS.

- [ ] **Step 5: Build (compile gate)**

Run: `cd desktop && npm run build`
Expected: build succeeds (no compile/type errors).

- [ ] **Step 6: Commit**

```bash
git add desktop/src/lib/configClient.ts desktop/src/lib/configClient.test.ts
git commit -m "feat(config-editor): profile CRUD helpers (profileNames/createProfile/deleteProfile)"
```

---

### Task 2: configClient — profile `extends` / `servers` readers + setters

**Files:**
- Modify: `desktop/src/lib/configClient.ts` (append after Task 1's block)
- Test: `desktop/src/lib/configClient.test.ts` (append a `describe`)

**Interfaces:**
- Consumes: `ConfigPayload`, `clone`/`asDict`/`strList` (already in file).
- Produces:
  - `profileExtends(payload, name): string` — `profiles[name].extends` or `"default"` when absent/non-string.
  - `setProfileExtends(payload, name, extendsName): ConfigPayload` — NEW payload setting `profiles[name].extends` (scalar; `"default"` is written literally, equals the default — exempt from absent≠empty).
  - `profileServers(payload, name): string[]` — `profiles[name].servers` as a string list (absent → `[]`).
  - `setProfileServers(payload, name, servers): ConfigPayload` — NEW payload setting `profiles[name].servers`, or OMITTING the key when the list is empty (absent → inherit base servers; explicit `[]` = serverless profile is řez 4b-ii). Input untouched.

- [ ] **Step 1: Write the failing tests**

Append to `desktop/src/lib/configClient.test.ts`:

```typescript
import { profileExtends, setProfileExtends, profileServers, setProfileServers } from "./configClient";

describe("profile extends / servers", () => {
  it("profileExtends reads the extends target or defaults to 'default'", () => {
    const p = parseConfig({ profiles: { a: { extends: "b" }, b: {} } })!;
    expect(profileExtends(p, "a")).toBe("b");
    expect(profileExtends(p, "b")).toBe("default"); // absent → default
  });

  it("setProfileExtends sets the scalar on a copy", () => {
    const p = parseConfig({ profiles: { a: {} } })!;
    const next = setProfileExtends(p, "a", "b");
    expect(profileExtends(next, "a")).toBe("b");
    expect(profileExtends(p, "a")).toBe("default"); // input untouched
  });

  it("profileServers reads the servers list or []", () => {
    const p = parseConfig({ profiles: { a: { servers: ["local", "vps"] }, b: {} } })!;
    expect(profileServers(p, "a")).toEqual(["local", "vps"]);
    expect(profileServers(p, "b")).toEqual([]); // absent → []
  });

  it("setProfileServers writes a non-empty list", () => {
    const p = parseConfig({ profiles: { a: {} } })!;
    const next = setProfileServers(p, "a", ["local"]);
    expect(profileServers(next, "a")).toEqual(["local"]);
    expect(profileServers(p, "a")).toEqual([]); // input untouched
  });

  it("setProfileServers OMITS the key when empty (→ inherit base servers, not [])", () => {
    const p = parseConfig({ profiles: { a: { servers: ["local"], extends: "default" } } })!;
    const next = setProfileServers(p, "a", []);
    expect("servers" in (next.profiles.a as Record<string, unknown>)).toBe(false); // omitted, not []
    expect((next.profiles.a as Record<string, unknown>).extends).toBe("default"); // sibling kept
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd desktop && npx vitest run src/lib/configClient.test.ts`
Expected: FAIL — the four helpers are not exported.

- [ ] **Step 3: Implement the helpers**

Append to `desktop/src/lib/configClient.ts` (after Task 1's profile block):

```typescript
/** The `extends` target of profile `name` ("default" = inherit base, when absent). */
export function profileExtends(payload: ConfigPayload, name: string): string {
  const ext = asDict(payload.profiles[name]).extends;
  return typeof ext === "string" ? ext : "default";
}

/** NEW payload with profile `name`'s `extends` set. A scalar — "default" is written
 *  literally (equals the default), so it is exempt from absent≠empty. Input untouched. */
export function setProfileExtends(
  payload: ConfigPayload,
  name: string,
  extendsName: string,
): ConfigPayload {
  const profiles = clone(payload.profiles);
  const overlay = { ...asDict(profiles[name]) };
  overlay.extends = extendsName;
  profiles[name] = overlay;
  return { ...payload, profiles };
}

/** Profile `name`'s `servers` list (absent → []). */
export function profileServers(payload: ConfigPayload, name: string): string[] {
  return strList(asDict(payload.profiles[name]).servers);
}

/** NEW payload with profile `name`'s `servers` set, or the key OMITTED when the list
 *  is empty. An absent `servers` means "inherit base servers"; an explicit `[]` (a
 *  serverless profile) is řez 4b-ii's presence-aware authoring — out of scope here.
 *  Input untouched. */
export function setProfileServers(
  payload: ConfigPayload,
  name: string,
  servers: string[],
): ConfigPayload {
  const profiles = clone(payload.profiles);
  const overlay = { ...asDict(profiles[name]) };
  if (servers.length === 0) delete overlay.servers;
  else overlay.servers = servers;
  profiles[name] = overlay;
  return { ...payload, profiles };
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd desktop && npx vitest run src/lib/configClient.test.ts`
Expected: PASS.

- [ ] **Step 5: Build (compile gate)**

Run: `cd desktop && npm run build`
Expected: build succeeds.

- [ ] **Step 6: Commit**

```bash
git add desktop/src/lib/configClient.ts desktop/src/lib/configClient.test.ts
git commit -m "feat(config-editor): profile extends/servers readers + setters"
```

---

### Task 3: configClient — `referencedTokenEnvs`, `orphanedSecrets`, `parseActiveChanged`

**Files:**
- Modify: `desktop/src/lib/configClient.ts` (append after Task 2's block)
- Test: `desktop/src/lib/configClient.test.ts` (append a `describe`)

**Interfaces:**
- Consumes: `ConfigPayload`, `obj` (already in file).
- Produces:
  - `referencedTokenEnvs(payload): Set<string>` — every non-blank `token_env` string anywhere in `base` + `profiles` (servers, base/profile telegram, profile overlays). Mirrors the backend collector.
  - `orphanedSecrets(payload): string[]` — keychain-backed (`source==="keychain"`), currently-set (`set===true`) secret names that NO `token_env` in the config still references — cleanup candidates after a rename/delete. (env-sourced and unset secrets are excluded.)
  - `parseActiveChanged(raw): boolean` — `{changed: true}` → `true`, else `false`.

- [ ] **Step 1: Write the failing tests**

Append to `desktop/src/lib/configClient.test.ts`:

```typescript
import { referencedTokenEnvs, orphanedSecrets, parseActiveChanged } from "./configClient";

describe("token-env references / orphaned secrets / active-changed", () => {
  it("referencedTokenEnvs collects token_env from servers, telegram, and profiles", () => {
    const p = parseConfig({
      base: {
        servers: [{ id: "a", url: "ws://x", token_env: "TOK" }],
        notifications: { telegram: { token_env: "TG", chat_id: "1" } },
      },
      profiles: { mobile: { notifications: { telegram: { token_env: "PTG" } } } },
    })!;
    expect(referencedTokenEnvs(p)).toEqual(new Set(["TOK", "TG", "PTG"]));
  });

  it("referencedTokenEnvs ignores blank token_env and non-strings", () => {
    const p = parseConfig({ base: { servers: [{ id: "a", url: "ws://x", token_env: "" }] } })!;
    expect(referencedTokenEnvs(p).size).toBe(0);
  });

  it("orphanedSecrets returns keychain-set names no token_env references", () => {
    const p = parseConfig({
      base: { servers: [{ id: "a", url: "ws://x", token_env: "TOK" }] },
      secrets: {
        TOK: { set: true, source: "keychain" }, // still referenced → not orphan
        OLD: { set: true, source: "keychain" }, // referenced by nothing → orphan
        ENVY: { set: true, source: "env" },     // env-sourced → never an orphan we clear
        GONE: { set: false, source: "keychain" }, // not set → excluded
      },
    })!;
    expect(orphanedSecrets(p)).toEqual(["OLD"]);
  });

  it("parseActiveChanged reads {changed: bool}", () => {
    expect(parseActiveChanged({ changed: true })).toBe(true);
    expect(parseActiveChanged({ changed: false })).toBe(false);
    expect(parseActiveChanged(null)).toBe(false);
    expect(parseActiveChanged("nope")).toBe(false);
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd desktop && npx vitest run src/lib/configClient.test.ts`
Expected: FAIL — the three helpers are not exported.

- [ ] **Step 3: Implement the helpers**

Append to `desktop/src/lib/configClient.ts` (after Task 2's block):

```typescript
/** Every non-blank `token_env` string referenced anywhere in base + profiles
 *  (servers, base/profile telegram, profile overlays). Mirrors the backend's
 *  `_collect_token_envs` so the editor can spot keychain entries gone orphan. */
export function referencedTokenEnvs(payload: ConfigPayload): Set<string> {
  const out = new Set<string>();
  const walk = (v: unknown): void => {
    if (Array.isArray(v)) {
      v.forEach(walk);
    } else if (v != null && typeof v === "object") {
      for (const [k, val] of Object.entries(v as Record<string, unknown>)) {
        if (k === "token_env" && typeof val === "string" && val !== "") out.add(val);
        else walk(val);
      }
    }
  };
  walk(payload.base);
  walk(payload.profiles);
  return out;
}

/** Keychain-backed secrets no `token_env` in the config still references — cleanup
 *  candidates after a rename/delete. env-sourced secrets (we can't clear them) and
 *  unset ones (nothing to clear) are excluded. */
export function orphanedSecrets(payload: ConfigPayload): string[] {
  const referenced = referencedTokenEnvs(payload);
  return Object.entries(payload.secrets)
    .filter(([name, flag]) => flag.set && flag.source === "keychain" && !referenced.has(name))
    .map(([name]) => name);
}

/** Parse the `{changed: bool}` reply from `config_set_active`. */
export function parseActiveChanged(raw: unknown): boolean {
  return obj(raw).changed === true;
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd desktop && npx vitest run src/lib/configClient.test.ts`
Expected: PASS.

- [ ] **Step 5: Build (compile gate)**

Run: `cd desktop && npm run build`
Expected: build succeeds.

- [ ] **Step 6: Commit**

```bash
git add desktop/src/lib/configClient.ts desktop/src/lib/configClient.test.ts
git commit -m "feat(config-editor): referencedTokenEnvs + orphanedSecrets + parseActiveChanged"
```

---

### Task 4: ProfilesSection + wire into ConfigApp

**Files:**
- Create: `desktop/src/lib/sections/ProfilesSection.svelte`
- Modify: `desktop/src/ConfigApp.svelte` (import + wire the `Profiles` branch)

**Interfaces:**
- Consumes: `profileNames`/`createProfile`/`deleteProfile`/`profileExtends`/`setProfileExtends`/`profileServers`/`setProfileServers` (Tasks 1–2), `serversOf` (exists), `SelectField` (exists); `ConfigPayload`.
- Produces: a section editing named profiles. Prop contract matches the other sections: `{ payload = $bindable(), onChange, onError }`. Profile names are immutable post-create (no rename in 4b-i) so `{#each names as name (name)}` keys by name safely.

- [ ] **Step 1: Create ProfilesSection.svelte**

```svelte
<script lang="ts">
  import SelectField from "../fields/SelectField.svelte";
  import {
    profileNames, createProfile, deleteProfile,
    profileExtends, setProfileExtends, profileServers, setProfileServers,
    serversOf, type ConfigPayload,
  } from "../configClient";

  let { payload = $bindable(), onChange, onError }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  let newName = $state("");

  const names = $derived(profileNames(payload));
  const serverIds = $derived(serversOf(payload).map((s) => s.id).filter((id) => id !== ""));

  // A profile may extend "default" (base) or any OTHER profile — never itself.
  function extendsOptions(self: string): string[] {
    return ["default", ...names.filter((n) => n !== self)];
  }

  function create(): void {
    const res = createProfile(payload, newName);
    if (!res.ok) { onError(res.error); return; }
    payload = res.payload;
    newName = "";
    onChange();
  }
  function remove(name: string): void {
    payload = deleteProfile(payload, name);
    onChange();
  }
  function setExtends(name: string, ext: string): void {
    payload = setProfileExtends(payload, name, ext);
    onChange();
  }
  function toggleServer(name: string, id: string, on: boolean): void {
    const cur = profileServers(payload, name);
    const next = on ? [...cur, id] : cur.filter((s) => s !== id);
    payload = setProfileServers(payload, name, next);
    onChange();
  }
</script>

<h2>Profiles</h2>
<p class="hint">Pojmenované profily překrývají bázi. Aktivní profil se vybírá nahoře; per-sekce overrides jsou řez 4b-ii.</p>

<div class="create">
  <input placeholder="jméno nového profilu" bind:value={newName} />
  <button type="button" onclick={create}>+ vytvořit profil</button>
</div>

{#each names as name (name)}
  <fieldset>
    <legend>{name} <button type="button" onclick={() => remove(name)}>×</button></legend>
    <SelectField
      label="extends"
      value={profileExtends(payload, name)}
      options={extendsOptions(name)}
      onchange={(v) => setExtends(name, v)}
    />
    <div class="servers">
      <span class="lbl">servers</span>
      {#if serverIds.length === 0}
        <span class="hint">žádné servery v bázi — přidej je v sekci Servers</span>
      {:else}
        {#each serverIds as id (id)}
          <label class="chk">
            <input
              type="checkbox"
              checked={profileServers(payload, name).includes(id)}
              onchange={(e) => toggleServer(name, id, (e.target as HTMLInputElement).checked)}
            />
            {id}
          </label>
        {/each}
      {/if}
    </div>
  </fieldset>
{/each}
{#if names.length === 0}
  <p class="hint">Zatím žádný profil. Vytvoř první výše.</p>
{/if}

<style>
  h2 { margin: 0 0 8px; }
  .hint { color: #888; margin: 0 0 8px; }
  .create { display: flex; gap: 6px; margin: 8px 0; }
  .create input { flex: 1; background: #141417; border: 1px solid #2a2a30; color: inherit; padding: 4px 6px; border-radius: 4px; }
  fieldset { border: 1px solid #2a2a30; border-radius: 6px; margin: 8px 0; padding: 8px 12px; }
  legend { color: #ccc; } legend button { color: #e05050; background: none; border: 0; cursor: pointer; }
  .servers { display: grid; grid-template-columns: 120px 1fr; gap: 8px; align-items: start; margin: 4px 0; }
  .servers .lbl { color: #aaa; }
  .chk { display: inline-flex; align-items: center; gap: 4px; margin-right: 12px; color: #ccc; }
  button { background: #1b1b1f; border: 1px solid #2a2a30; color: inherit; border-radius: 4px; padding: 4px 8px; cursor: pointer; }
</style>
```

- [ ] **Step 2: Wire the Profiles branch into ConfigApp.svelte**

In `desktop/src/ConfigApp.svelte`, add the import after the `AnswerProfilesSection` import (line 14):

```svelte
  import ProfilesSection from "./lib/sections/ProfilesSection.svelte";
```

Then replace the trailing `{:else}` placeholder block (currently lines 161–162, `{:else} <p class="hint">Sekce „{active}" — řez 4b.</p>`) with the Profiles branch followed by a generic fallback:

```svelte
      {:else if active === "Profiles"}
        <ProfilesSection bind:payload onChange={markDirty} onError={(m) => (notice = m)} />
      {:else}
        <p class="hint">Neznámá sekce „{active}".</p>
      {/if}
```

- [ ] **Step 3: Build (compile gate)**

Run: `cd desktop && npm run build`
Expected: 0 errors; build succeeds.

- [ ] **Step 4: Verify nothing regressed**

Run: `cd desktop && npx vitest run`
Expected: all pure-logic tests pass.

- [ ] **Step 5: Commit**

```bash
git add desktop/src/lib/sections/ProfilesSection.svelte desktop/src/ConfigApp.svelte
git commit -m "feat(config-editor): Profiles section (create/delete + extends + servers)"
```

---

### Task 5: Banner component + ConfigApp `notice` → structured `banner`

**Files:**
- Create: `desktop/src/lib/Banner.svelte`
- Modify: `desktop/src/ConfigApp.svelte` (replace the `notice: string` state with a `banner` object + render via `Banner`)

**Interfaces:**
- Consumes: nothing (presentational).
- Produces: `Banner` props `{ kind?: "warning" | "error" | "success"; message: string; actionLabel?: string; onAction?: () => void }` — renders nothing when `message` is empty; otherwise a colored bar with the message and an optional action button. In `ConfigApp`, the single `notice` string becomes a `banner` object `{ kind, message, actionLabel?, onAction? } | null` set through a `setBanner(...)` helper; every prior `notice = "…"` / `notice = ""` site is migrated. Task 7 reuses `banner` for the orphan-cleanup action.

- [ ] **Step 1: Create Banner.svelte**

```svelte
<script lang="ts">
  let { kind = "warning", message, actionLabel, onAction }:
    {
      kind?: "warning" | "error" | "success";
      message: string;
      actionLabel?: string;
      onAction?: () => void;
    } = $props();
</script>

{#if message}
  <div class="banner {kind}">
    <span class="msg">{message}</span>
    {#if actionLabel}
      <button type="button" onclick={() => onAction?.()}>{actionLabel}</button>
    {/if}
  </div>
{/if}

<style>
  .banner { display: flex; align-items: center; gap: 8px; padding: 4px 8px; border-radius: 4px; }
  .banner .msg { flex: 1; }
  .warning { background: #2a2410; color: #e0a030; }
  .error { background: #2a1414; color: #e05050; }
  .success { background: #14241a; color: #4caf78; }
  .banner button { background: #1b1b1f; border: 1px solid #2a2a30; color: inherit; border-radius: 4px; padding: 2px 8px; cursor: pointer; }
</style>
```

- [ ] **Step 2: Migrate ConfigApp's `notice` string to a `banner` object**

In `desktop/src/ConfigApp.svelte`:

(a) Add the import after the `ProfilesSection` import (added in Task 4):

```svelte
  import Banner from "./lib/Banner.svelte";
```

(b) Replace the `notice` state declaration (line 36, `let notice = $state(""); // …`) with the banner state + helper:

```svelte
  // A structured status banner (replaces the old plain `notice` string). Task 7
  // reuses the optional action for the orphaned-keychain-secret cleanup.
  type BannerState = { kind: "warning" | "error" | "success"; message: string; actionLabel?: string; onAction?: () => void };
  let banner = $state<BannerState | null>(null);
  function setBanner(kind: BannerState["kind"], message: string, actionLabel?: string, onAction?: () => void): void {
    banner = { kind, message, actionLabel, onAction };
  }
```

(c) Migrate every `notice` site:
- In `load()`: the two `notice = "…"` assignments become `setBanner("warning", "…")` (keep the exact strings), and the `notice = ""` on success becomes `banner = null`:

```svelte
  async function load(): Promise<void> {
    try {
      const fresh = parseConfig(await cfg.read());
      if (fresh == null) {
        setBanner("warning", "neočekávaná odpověď configu ze sidecaru");
        return;
      }
      payload = fresh;
      dirty = false;
      errors = [];
      banner = null;
      reloadRev += 1;
    } catch {
      setBanner(
        "warning",
        payload == null
          ? "sidecar zatím neběží — zkouším znovu…"
          : "obnovení configu ze sidecaru selhalo (neuložené změny zůstávají)",
      );
    }
  }
```

- Every section's `onError={(m) => (notice = m)}` (all branches in the `{#if active === ...}` ladder) becomes `onError={(m) => setBanner("error", m)}`.

(d) In the savebar, replace the inline notice span (line 173, `{#if notice}<span class="notice">{notice}</span>{/if}`) with the Banner, and drop the now-unused `.notice` CSS rule:

```svelte
    {#if banner}<Banner kind={banner.kind} message={banner.message} actionLabel={banner.actionLabel} onAction={banner.onAction} />{/if}
```

- [ ] **Step 3: Build (compile gate)**

Run: `cd desktop && npm run build`
Expected: 0 errors. (Grep first to confirm no stray `notice` reference remains: `grep -n "notice" desktop/src/ConfigApp.svelte` should return nothing.)

- [ ] **Step 4: Commit**

```bash
git add desktop/src/lib/Banner.svelte desktop/src/ConfigApp.svelte
git commit -m "feat(config-editor): structured Banner; migrate notice string to banner state"
```

---

### Task 6: Functional profile switcher (env-lock + dirty guard)

**Files:**
- Modify: `desktop/src/ConfigApp.svelte` (the topbar `<select>` + an `onchange` handler)

**Interfaces:**
- Consumes: `parseActiveChanged` (Task 3), `cfg.setActive` (exists), `setBanner` (Task 5); `payload.envLocked`/`payload.activeProfile` (from řez 4a).
- Produces: the topbar profile `<select>` becomes functional — its value reflects `activeProfile`, selecting a different one calls `config_set_active` and re-loads on success; it is disabled when env-locked or dirty, each with a hint.

**Design notes (not steps):** The existing `profiles` derived is `["default (báze)", ...Object.keys(payload.profiles)]`. The select's value maps `activeProfile` (`"default"` ⇄ the `"default (báze)"` label). Because the switcher is disabled while `dirty`, a freshly-created-but-unApplied profile (which appears in `payload.profiles`) can never be selected until it is Applied — so no separate "persisted-only" filtering is needed.

- [ ] **Step 1: Add the switch handler + value mapping**

In `desktop/src/ConfigApp.svelte`, add after the `profiles` derived (line 41):

```svelte
  const DEFAULT_LABEL = "default (báze)";
  // The label currently selected in the switcher (maps the effective active profile).
  const activeLabel = $derived(
    payload == null || payload.activeProfile === "default" ? DEFAULT_LABEL : payload.activeProfile,
  );
  const switcherDisabled = $derived(payload == null || payload.envLocked || dirty);

  async function switchProfile(label: string): Promise<void> {
    if (!payload) return;
    const name = label === DEFAULT_LABEL ? "default" : label;
    if (name === payload.activeProfile) return; // no-op: same profile
    try {
      const changed = parseActiveChanged(await cfg.setActive(name));
      if (changed) {
        await load(); // re-read saved state; preview refreshes via its own poll
      } else {
        setBanner("warning", `profil '${name}' nelze aktivovat (zamčen nebo neznámý)`);
      }
    } catch (e) {
      setBanner("error", `přepnutí profilu selhalo: ${String(e)}`);
    }
  }
```

- [ ] **Step 2: Make the topbar `<select>` functional**

Replace the topbar profile control (lines 121–126) with:

```svelte
    <label>
      Profil:
      <select
        value={activeLabel}
        disabled={switcherDisabled}
        onchange={(e) => switchProfile((e.target as HTMLSelectElement).value)}
      >
        {#each profiles as p}<option value={p}>{p}</option>{/each}
      </select>
    </label>
    {#if payload?.envLocked}
      <span class="hint">profil zamčen přes HERDECK_PROFILE</span>
    {:else if dirty}
      <span class="hint">ulož nebo zahoď změny pro přepnutí profilu</span>
    {/if}
```

(The `.hint` color rule already exists in `ConfigApp`'s `<style>`.)

- [ ] **Step 3: Build (compile gate)**

Run: `cd desktop && npm run build`
Expected: 0 errors; build succeeds.

- [ ] **Step 4: Commit**

```bash
git add desktop/src/ConfigApp.svelte
git commit -m "feat(config-editor): functional profile switcher (set_active + env-lock + dirty guard)"
```

---

### Task 7: Apply-time orphaned-keychain-secret cleanup

**Files:**
- Modify: `desktop/src/ConfigApp.svelte` (after a successful Apply, surface orphans via the banner action)

**Interfaces:**
- Consumes: `orphanedSecrets` (Task 3), `cfg.clearSecret` (exists), `setBanner`/`banner` (Task 5); `ConfigPayload`.
- Produces: after a successful Apply, if the saved config has keychain secrets no `token_env` still references (e.g. after a `token_env` rename or a server/profile delete), the banner offers a one-click cleanup that clears each via `config_secret_clear` and updates the presence flags. No value is read or migrated.

**Design note:** `apply()` already calls `load()` after a clean write, so by the time we check, `payload` reflects the saved config. Compute orphans from that fresh `payload`.

- [ ] **Step 1: Add the orphan-cleanup flow to `apply()`**

In `desktop/src/ConfigApp.svelte`, add the cleanup helper after `apply()` (near line 85):

```svelte
  async function cleanupOrphans(names: string[]): Promise<void> {
    if (!payload) return;
    const secrets = { ...payload.secrets };
    for (const name of names) {
      const code = await cfg.clearSecret(name);
      if (code === 204) secrets[name] = { set: false, source: null };
      else { setBanner("error", `úklid tokenu '${name}' selhal (HTTP ${code})`); return; }
    }
    payload = { ...payload, secrets };
    setBanner("success", "osiřelé keychain klíče uklizeny");
  }
```

Then, in `apply()`, after the successful-write `await load();` (line 78), add the orphan check (the banner shown here outlives `load()`'s `banner = null` because this runs after it):

```svelte
      if (res.length === 0) {
        dirty = false;
        await load(); // re-read saved state (preview refreshes itself via its own poll)
        const orphans = payload ? orphanedSecrets(payload) : [];
        if (orphans.length > 0) {
          setBanner(
            "warning",
            `${orphans.length} osiřelých keychain klíčů (${orphans.join(", ")})`,
            "uklidit",
            () => void cleanupOrphans(orphans),
          );
        }
      }
```

Add `orphanedSecrets` to the `configClient` import block at the top of the file (the one importing `parseConfig`/`toWriteBody`/…):

```svelte
    orphanedSecrets,
```

- [ ] **Step 2: Build (compile gate)**

Run: `cd desktop && npm run build`
Expected: 0 errors; build succeeds.

- [ ] **Step 3: Run the desktop suite (no regression)**

Run: `cd desktop && npx vitest run`
Expected: all pure-logic tests pass.

- [ ] **Step 4: Commit**

```bash
git add desktop/src/ConfigApp.svelte
git commit -m "feat(config-editor): Apply-time cleanup of orphaned keychain secrets"
```

---

### Task 8: Deferred fix F-2 — percent-encode the secret-clear path (Rust + Python)

**Files:**
- Modify: `desktop/src-tauri/src/http.rs` (add `percent_encode_segment` + a unit test)
- Modify: `desktop/src-tauri/src/lib.rs` (`config_secret_clear` encodes the segment)
- Modify: `src/herdeck/deckapp/server.py` (`do_DELETE` `unquote`s the segment)
- Test: `tests/test_deckapp.py` (route test: encoded `token_env` is decoded before `clear_secret`)

**Interfaces:**
- Consumes: nothing new.
- Produces: `http::percent_encode_segment(s: &str) -> String` — RFC 3986 path-segment encoding (keeps `A-Za-z0-9-._~`, `%XX` for everything else). `config_secret_clear` builds `/secret/{encoded}`; the sidecar `unquote`s it back. A `token_env` with a space/slash now round-trips instead of breaking the request line or the sidecar's `rsplit('/')`.

- [ ] **Step 1: Write the failing Rust unit test**

In `desktop/src-tauri/src/http.rs`, add inside the existing `#[cfg(test)] mod tests { ... }` block (after `base64_encode_matches_rfc_vectors`):

```rust
    #[test]
    fn percent_encode_segment_encodes_unsafe_and_keeps_unreserved() {
        assert_eq!(percent_encode_segment("TOK"), "TOK");
        assert_eq!(percent_encode_segment("My_Tok-1.0~x"), "My_Tok-1.0~x");
        assert_eq!(percent_encode_segment("MY TOK"), "MY%20TOK");
        assert_eq!(percent_encode_segment("a/b"), "a%2Fb");
        assert_eq!(percent_encode_segment("é"), "%C3%A9"); // UTF-8 bytes, upper-hex
    }
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd desktop/src-tauri && ~/.cargo/bin/cargo test percent_encode_segment`
Expected: FAIL to COMPILE — `percent_encode_segment` is not defined.

- [ ] **Step 3: Implement `percent_encode_segment`**

In `desktop/src-tauri/src/http.rs`, add next to `base64_encode` (e.g. just before it, around line 276):

```rust
/// Percent-encode a single URL path segment per RFC 3986: keep the unreserved
/// set (`A-Z a-z 0-9 - . _ ~`), emit `%XX` (upper-hex) for every other byte.
/// Used so a `token_env` with a space or slash can't break the DELETE request
/// line or the sidecar's `path.rsplit('/')`. The sidecar `unquote`s it back.
pub fn percent_encode_segment(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for &b in s.as_bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}
```

- [ ] **Step 4: Run the Rust test to verify it passes**

Run: `cd desktop/src-tauri && ~/.cargo/bin/cargo test percent_encode_segment`
Expected: PASS.

- [ ] **Step 5: Use the encoder in `config_secret_clear`**

In `desktop/src-tauri/src/lib.rs`, change the path build in `config_secret_clear` (line 220) from:

```rust
        &format!("/secret/{token_env}"),
```

to:

```rust
        &format!("/secret/{}", http::percent_encode_segment(&token_env)),
```

- [ ] **Step 6: Write the failing backend route test**

In `tests/test_deckapp.py`, add after `test_delete_secret_route_clears` (around line 543):

```python
def test_delete_secret_route_unquotes_path_segment():
    """A percent-encoded token_env (space/slash) must be DECODED before clear_secret,
    so the Rust-side percent-encoding (F-2) targets the real keychain key, not '%20'."""
    stub = _StubConfigService()
    app = create_mock_app(port=0, icon_provider=StubIcons(), config_service=stub)
    try:
        # %20 = space; the clear must target the decoded name "MY TOK".
        url = f"http://{app.host}:{app.port}/secret/MY%20TOK"
        req = urllib.request.Request(
            url, method="DELETE", headers={"X-Herdeck-Token": app.token}
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            assert r.status == 204
        assert stub.cleared == ["MY TOK"]
    finally:
        app.close()
```

- [ ] **Step 7: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_deckapp.py::test_delete_secret_route_unquotes_path_segment -v`
Expected: FAIL — `stub.cleared == ["MY%20TOK"]` (the raw, undecoded segment).

- [ ] **Step 8: Implement the sidecar `unquote`**

In `src/herdeck/deckapp/server.py`:

(a) Add `unquote` to the existing `urllib.parse` import (it already imports `urlsplit`). Change:

```python
from urllib.parse import urlsplit
```

to:

```python
from urllib.parse import unquote, urlsplit
```

(If `urlsplit` is imported on a shared line with other names, just add `unquote` to that line — keep it sorted: `unquote, urlsplit`.)

(b) In `do_DELETE` (line 398), decode the segment before clearing:

```python
                    app._config_service.clear_secret(unquote(path.rsplit("/", 1)[1]))
```

- [ ] **Step 9: Run the backend test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_deckapp.py::test_delete_secret_route_unquotes_path_segment tests/test_deckapp.py::test_delete_secret_route_clears -v`
Expected: PASS (both — the existing `MYTOK` case is unaffected since `unquote("MYTOK") == "MYTOK"`).

- [ ] **Step 10: Verify CI parity (both sides)**

Run: `.venv/bin/ruff check src tests && .venv/bin/python -m pytest tests/test_deckapp.py -q && (cd desktop/src-tauri && ~/.cargo/bin/cargo test)`
Expected: `All checks passed!` + pytest green + cargo green.

- [ ] **Step 11: Commit**

```bash
git add desktop/src-tauri/src/http.rs desktop/src-tauri/src/lib.rs src/herdeck/deckapp/server.py tests/test_deckapp.py
git commit -m "fix(config-editor): percent-encode + unquote DELETE /secret/{token_env} path"
```

---

## Self-Review

**1. Spec coverage** (against `2026-06-26-config-editor-frontend-4b-i-design.md`):
- **A — Profiles section** (list/create/delete + `extends` + `servers`): Tasks 1–2 (helpers) + Task 4 (UI). ✅
- **A — switcher** (set_active + env-lock + dirty guard): Task 3 (`parseActiveChanged`) + Task 6. ✅
- **E — error/toast polish** (notice → structured banner): Task 5. ✅
- **F-1 — keychain orphan** (frontend-only Apply-time cleanup): Task 3 (`orphanedSecrets`) + Task 7. ✅
- **F-2 — DELETE percent-encode** (Rust encode + Python unquote, round-trip tested): Task 8. ✅
- **Out of scope (řez 4b-ii):** OverrideField three-state, per-section overlay editing, explicit-empty authoring, klik-to-jump — none implemented here; switcher is active-only and forms stay base (Global Constraints). ✅
- **No new Tauri commands / HTTP routes:** CRUD via model+Apply; `config_set_active`/`config_secret_clear` reused. ✅
- **Absent ≠ empty:** `setProfileServers` omits an empty `servers` (→ inherit); `extends` scalar exempt. ✅

**2. Placeholder scan:** No "TBD/handle-edge-cases". The keychain-orphan UX is concrete (Apply-time banner action clearing keychain entries; value never migrated). The F-2 fix is fully specified on both sides with a round-trip test. Every component's full code is inline; no "similar to Task N".

**3. Type consistency:**
- `ProfileResult` (Task 1) returned by `createProfile`, consumed in `ProfilesSection.create()` (Task 4) via the `res.ok` discriminant. ✅
- `profileExtends`/`setProfileExtends`/`profileServers`/`setProfileServers` (Task 2) signatures used identically in `ProfilesSection` (Task 4). ✅
- `orphanedSecrets(payload): string[]` (Task 3) consumed in `apply()` (Task 7); `parseActiveChanged(raw): boolean` (Task 3) consumed in `switchProfile()` (Task 6). ✅
- Section prop contract `{ payload = $bindable(), onChange, onError }` matches how `ConfigApp` wires the Profiles branch (`bind:payload onChange={markDirty} onError={(m) => setBanner("error", m)}` after the Task-5 migration). ✅
- `BannerState`/`setBanner` (Task 5) used by Tasks 6 & 7; `Banner` props `{kind, message, actionLabel?, onAction?}` match the `ConfigApp` render site. ✅
- `percent_encode_segment` (Task 8) used in `lib.rs` `config_secret_clear`; the sidecar `unquote` is its inverse — round-trip asserted. ✅

## Execution Handoff

Plan complete. Recommended execution: **subagent-driven** (fresh implementer per task + per-task spec+quality review + roborev, opus final whole-branch review), in-place on a feature branch off `6893a03` — exactly as řez 4a was executed. Note Task 8 touches Rust + Python (run `cargo test` AND `pytest`); Tasks 1–7 are desktop-only (vitest + `npm run build`).
