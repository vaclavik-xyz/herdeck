# Config editor frontend — řez 4a (base-mode sections) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every config section editable in **base mode** in the config editor window — Deck (+ local Hardware), View, Theme, Macros, Start profiles, Notifications, Safety, Answer profiles — plus the shared field widgets, the onboarding empty-state, and the additive `env_locked`/`active_profile` backend field — building directly on the řez-3 transport + shell + Servers section.

**Architecture:** Thin GUI over the existing backend API. Each section is a Svelte 5 component that edits the in-memory `ConfigPayload` (the parsed `GET /config` shape) through pure, unit-tested helpers in `configClient.ts`, then the existing global Apply (`POST /config`) persists the whole `{base, profiles, local}`. No new Rust commands and no new HTTP routes — the read/validate/write/secret commands from řez 3 cover everything here. Profiles/overlay editing, the profile switcher wiring, klik-to-jump, and error-banner polish are **out of scope** (řez 4b).

**Tech Stack:** Tauri 2 (Rust, unchanged here), Svelte 5 runes (`$state`/`$derived`/`$props`/`$bindable`), TypeScript, Vitest (pure-logic tests), Python 3.12+ (one backend task), ruff + pytest.

## Global Constraints

These bind every task. Copied from `docs/superpowers/specs/2026-06-25-config-editor-frontend-design.md` and the established řez-3 patterns.

- **No new config logic in the frontend.** The editor is a thin layer over the backend API; `configClient.ts` only maps the `read()` payload to/from editor structures. No config resolution, merging, or validation is reimplemented in JS — validation is the backend's (`POST /config` / `/config/validate`).
- **The sidecar token NEVER lives in JS.** Every backend call goes through the existing token-free Tauri commands (`config_read`/`config_validate`/`config_write`/`config_secret_set`/`config_secret_clear`); the Rust shell injects the token. No `fetch()` to the sidecar, no token in any component. (Řez 4a adds NO new Rust commands.)
- **Secret VALUES are one-way and never in the model, TOML, response body, or logs.** `TokenSecretField` shows only the env-var NAME + a `{set, source}` presence flag; a value is sent via `config_secret_set` (→ `POST /secret`, keychain) and never read back. The editor model (`ConfigPayload`) carries `secrets` as presence flags only.
- **Save model = explicit global Apply.** Sections mutate the in-memory `payload` (`$bindable`) and call `onChange()` → marks dirty. Nothing is written until the user clicks **Apply**, which POSTs the whole `{base, profiles, local}`. No auto-save, no per-section save, no live preview of unsaved edits.
- **Hardware is local-only.** Hardware fields live in `local.toml` as `[local]` (deck kind, herdr_socket, web_bind, web_port, icons_dir) and `[hardware]` (brightness, debounce, keep_alive_interval, tick_interval). They are edited as part of the `local` payload (`payload.local.local.*` / `payload.local.hardware.*`), written to `local.toml`, and are NEVER offered as a base/profile `[deck]` field.
- **Pure logic = TDD (Vitest); Svelte components = build gate (+ compile-smoke for not-yet-imported widgets).** Pure functions in `configClient.ts` and the Python backend follow strict TDD (failing test first). Svelte components are verified by `npm run build` (vite compiles the Svelte + TS and reports compile/type errors). But `vite build` only compiles the REACHABLE module graph, so a freshly-created widget that no section imports yet would slip through. For those (Tasks 5–6) add a one-line Vitest **compile-smoke** that imports the widget — the vitest config already loads the `svelte()` plugin under jsdom, so importing a `.svelte` compiles it. A compile-smoke is just an import + truthy check; do NOT add `svelte-check`, and do NOT build a full render/interaction component-test harness (řez 3 set that precedent — match it).
- **Backend payload shapes are used verbatim.** `read()` returns `{base, profiles, local, secrets}`; `base` holds the present base sections; `local` holds `{active_profile, local: {...}, hardware: {...}}`; `secrets` is `{name: {set, source}}`. Write/validate take `{base, profiles, local}`.
- **Absent ≠ empty (the defaults rule).** In this config an ABSENT key means "use the backend default" (`DEFAULT_BOTTOM_ROW`, `DEFAULT_MACROS`, `DEFAULT_START_PROFILES`, all servers for `overview_order`, …); an explicit `[]`/`{}` means "none" and DISABLES that default. So řez 4a never materializes a default into an explicit empty: an emptied list/collection editor maps to "return to default" by OMITTING the key (`putList` for scalar-section lists; `applyMapSection`/`withMacros` for collections). Authoring an INTENTIONAL explicit-empty value (e.g. `view.tile_primary = []` to switch a tile line off, or an empty `[start_profiles]` to disable all launchers) needs a three-state default/set/empty UX — that is **řez 4b's presence-aware `OverrideField`**, out of scope here. Scalars are exempt (their explicit value equals the default value, so writing it changes nothing).
- **Base mode only.** The profile switcher stays the disabled skeleton from řez 3. Section components read/write `base` (and Hardware writes `local`). No `OverrideField`, no overlay, no profile CRUD here. Keep section internals simple and concrete (mirror `ServersSection.svelte`); řez 4b layers overlay on top.
- **CI parity before any push:** `ruff check src tests` AND `python -m pytest` for Python changes; `npm run build` + `npx vitest run` for desktop changes (both repo-local: vite + vitest are declared devDeps). (Pushing is a separate, user-approved step; this is the per-task gate.)

## File Structure

New and modified files, by responsibility:

| File | Status | Responsibility |
|---|---|---|
| `src/herdeck/deckapp/config_service.py` | modify | `read()` additionally returns `env_locked: bool` + effective `active_profile: str`; loads `local.toml` even when `config.toml` is absent |
| `tests/test_config_service.py` | modify | tests for the two new `read()` fields incl. the config-missing-but-local-present case |
| `desktop/src/lib/configClient.ts` | modify | `ConfigPayload` gains `envLocked`/`activeProfile`; new pure helpers `getAt`/`setAt`/`removeAt` (root/section/key access for `base` + `local`), macros mutators, and the tested map-section serialization core (`startProfileRows`/`answerProfileRows`/`serializeNamedRows`/`applyMapSection`) |
| `desktop/src/lib/configClient.test.ts` | modify | tests for the parse additions, `getAt`/`setAt`/`removeAt`, macros mutators |
| `desktop/src/lib/fields/NumberField.svelte` | create | numeric scalar widget (int/float, blank → null) |
| `desktop/src/lib/fields/BooleanField.svelte` | create | checkbox widget |
| `desktop/src/lib/fields/SelectField.svelte` | create | enum dropdown widget |
| `desktop/src/lib/fields/ListField.svelte` | create | string-list editor (add/remove/edit rows) |
| `desktop/src/lib/fields/widgets.smoke.test.ts` | create | Vitest compile-smoke importing the new widgets (so not-yet-imported widgets are actually compiled) |
| `desktop/src/lib/sections/DeckSection.svelte` | create | base `[deck]` (grid, overview_order) + local-only Hardware subsection |
| `desktop/src/lib/sections/ViewSection.svelte` | create | base `[view]` (management, agent_slots, show_profile_on_panel, bottom_row, tile_fields, tile_primary, tile_secondary) |
| `desktop/src/lib/sections/ThemeSection.svelte` | create | base `[theme]` (six fixed status colors + server_accents list) |
| `desktop/src/lib/sections/MacrosSection.svelte` | create | base `[[macros]]` (label/text records) |
| `desktop/src/lib/sections/StartProfilesSection.svelte` | create | base `[start_profiles]` (name → argv list map) |
| `desktop/src/lib/sections/NotificationsSection.svelte` | create | base `[notifications]` (+ telegram token_env secret + chat_id) |
| `desktop/src/lib/sections/SafetySection.svelte` | create | base `[safety]` (approve_always, require_confirm_for) |
| `desktop/src/lib/sections/AnswerProfilesSection.svelte` | create | base `[answer_profiles]` (name → {approve,deny,stop,approve_always} lists) |
| `desktop/src/ConfigApp.svelte` | modify | wire each new section into the `{#if active === ...}` ladder; `reloadRev` signal (re-seeds map sections' local rows on load/discard); onboarding empty-state; distinguish load error from empty config |

---

### Task 1: Backend — `read()` returns `env_locked` + effective `active_profile`

**Files:**
- Modify: `src/herdeck/deckapp/config_service.py:36-52` (the `read()` method)
- Test: `tests/test_config_service.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `ConfigService.read()` payload gains two keys — `env_locked: bool` (`True` iff `HERDECK_PROFILE` env var is set) and `active_profile: str` (the effective active profile: env profile if locked, else `local.toml` `active_profile`, else base `active_profile`, else `"default"`). `local.toml` is loaded even when `config.toml` is absent, so a local-only setup keeps its `local`/`active_profile`. A legacy top-level `active_profile` in `config.toml` is also carried inside `base` so it round-trips through `write()` (otherwise editing+Applying any section would drop it). A DANGLING `local` `active_profile` (one naming a profile that does not exist — always the case with no `config.toml`) is dropped from the returned `local`, so the editor never round-trips an unresolvable selection that the first Apply would reject as "unknown profile" (řez 4a cannot edit it — the switcher is disabled); hardware/other local keys are kept. The frontend (Task 2) parses the two new top-level keys; řez 4b uses them.

- [ ] **Step 1: Update the existing onboarding test + add the new failing tests**

First, the existing `test_read_missing_config_is_empty_for_onboarding` (around line 97) asserts the OLD exact payload and will break once `read()` adds the two keys. Replace it (it gains `monkeypatch` to pin the env, and asserts the full new shape — this is the no-config-no-local case, so the separate `test_read_no_config_no_local_defaults` is NOT added, to avoid a duplicate):

```python
def test_read_missing_config_is_empty_for_onboarding(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDECK_PROFILE", raising=False)
    svc = ConfigService(tmp_path / "config.toml", tmp_path / "local.toml")
    assert svc.read() == {
        "base": {},
        "profiles": {},
        "local": {},
        "secrets": {},
        "env_locked": False,
        "active_profile": "default",
    }
```

Then add these new tests (the file already imports `secret_store` + `ConfigService` and has `_svc` + `_FakeKeyring`; mirror `test_read_returns_base_profiles_local`):

```python
def test_read_reports_env_locked_and_active_profile(tmp_path, monkeypatch):
    monkeypatch.delenv("TOK", raising=False)
    monkeypatch.delenv("TG", raising=False)
    monkeypatch.setattr(secret_store, "_keyring", _FakeKeyring)
    monkeypatch.setenv("HERDECK_PROFILE", "mobile")
    svc = _svc(tmp_path, local='active_profile = "work"\n')
    data = svc.read()
    assert data["env_locked"] is True
    assert data["active_profile"] == "mobile"  # env wins over local


def test_read_active_profile_falls_back_to_local_then_default(tmp_path, monkeypatch):
    monkeypatch.delenv("TOK", raising=False)
    monkeypatch.delenv("TG", raising=False)
    monkeypatch.delenv("HERDECK_PROFILE", raising=False)
    monkeypatch.setattr(secret_store, "_keyring", _FakeKeyring)
    # "mobile" exists in the _svc config profiles, so it is a valid (non-dangling) selection.
    svc = _svc(tmp_path, local='active_profile = "mobile"\n')
    data = svc.read()
    assert data["env_locked"] is False
    assert data["active_profile"] == "mobile"


def test_read_active_profile_defaults_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("TOK", raising=False)
    monkeypatch.delenv("TG", raising=False)
    monkeypatch.delenv("HERDECK_PROFILE", raising=False)
    monkeypatch.setattr(secret_store, "_keyring", _FakeKeyring)
    svc = _svc(tmp_path)  # no local.toml
    data = svc.read()
    assert data["env_locked"] is False
    assert data["active_profile"] == "default"


def test_read_no_config_drops_dangling_active_profile_keeps_hardware(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDECK_PROFILE", raising=False)
    # config.toml absent → no profiles exist, so a stale local active_profile ("work") is a
    # dangling reference that would block the first Apply. It is dropped (effective profile
    # falls back to default), but the rest of local (hardware) survives.
    (tmp_path / "local.toml").write_text('active_profile = "work"\n[hardware]\nbrightness = 55\n')
    svc = ConfigService(tmp_path / "config.toml", tmp_path / "local.toml")
    data = svc.read()
    assert data["base"] == {}
    assert data["profiles"] == {}
    assert data["secrets"] == {}
    assert data["env_locked"] is False
    assert data["active_profile"] == "default"  # dangling "work" normalized away
    assert "active_profile" not in data["local"]  # not round-tripped into write
    assert data["local"]["hardware"]["brightness"] == 55  # hardware preserved


def test_read_write_round_trip_preserves_top_level_active_profile(tmp_path, monkeypatch):
    # A legacy top-level active_profile in config.toml must survive an edit+write, not be
    # dropped (which would silently revert the effective profile to default).
    monkeypatch.setenv("TOK", "real")
    monkeypatch.delenv("HERDECK_PROFILE", raising=False)
    monkeypatch.setattr(secret_store, "_keyring", _FakeKeyring)
    (tmp_path / "config.toml").write_text(
        'active_profile = "work"\n'
        '[[servers]]\nid = "local"\nurl = "ws://x"\ntoken_env = "TOK"\n'
        '[deck]\ngrid = "5x3"\n'
        '[profiles.work]\nservers = ["local"]\n'  # the active profile must resolve
    )
    svc = ConfigService(tmp_path / "config.toml", tmp_path / "local.toml")
    data = svc.read()
    assert data["base"]["active_profile"] == "work"  # carried in base for round-trip
    errors = svc.write({"base": data["base"], "profiles": data["profiles"], "local": data["local"]})
    assert errors == []
    again = svc.read()
    assert again["base"]["active_profile"] == "work"  # survived the write
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_config_service.py -v`
Expected: FAIL — `KeyError: 'env_locked'` in the new tests; the updated onboarding test fails on the missing keys.

- [ ] **Step 3: Implement the additive read fields (loading local first)**

In `src/herdeck/deckapp/config_service.py`, replace the whole `read()` method (lines 36–52) with:

```python
    def read(self) -> dict:
        env_profile = os.environ.get("HERDECK_PROFILE")
        # Load local.toml unconditionally so a local-only setup (no config.toml)
        # still keeps its hardware overrides.
        local = (
            tomllib.loads(self._local_path.read_text(encoding="utf-8"))
            if self._local_path.exists()
            else {}
        )
        if self._config_path.exists():
            data = tomllib.loads(self._config_path.read_text(encoding="utf-8"))
            base = {sec: data[sec] for sec in self.BASE_SECTIONS if sec in data}
            # Carry a legacy top-level `active_profile` (the unified format also allows it
            # in config.toml) inside `base` so it round-trips through write() — otherwise
            # editing any section and Applying would drop it and revert the effective
            # profile. Inert to the section editors (no section is named "active_profile").
            if "active_profile" in data:
                base["active_profile"] = data["active_profile"]
            profiles = data.get("profiles", {})
        else:
            data, base, profiles = {}, {}, {}
        # Drop a DANGLING local active_profile — one that names a profile which does not
        # exist (e.g. config.toml is gone, or the profile was never defined). Otherwise the
        # editor round-trips an unresolvable selection and the first Apply is rejected as
        # "unknown profile", with no way to fix it in řez 4a (the switcher stays disabled).
        # "default" is always valid; other local keys (e.g. hardware) are kept.
        sel = local.get("active_profile")
        if sel is not None and sel != "default" and sel not in profiles:
            local = {k: v for k, v in local.items() if k != "active_profile"}
        active = (
            env_profile
            or local.get("active_profile")
            or data.get("active_profile")
            or "default"
        )
        return {
            "base": base,
            "profiles": profiles,
            "local": local,
            "secrets": self._secret_flags(base, profiles),
            "env_locked": env_profile is not None,
            "active_profile": active,
        }
```

(`os` is already imported at module top — line 8.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_config_service.py -v`
Expected: PASS (all config_service tests, including the five new ones).

- [ ] **Step 5: Verify CI parity**

Run: `ruff check src tests && python -m pytest tests/test_config_service.py tests/test_deckapp.py -q`
Expected: `All checks passed!` + green.

- [ ] **Step 6: Commit**

```bash
git add src/herdeck/deckapp/config_service.py tests/test_config_service.py
git commit -m "feat(config-service): read() reports env_locked + effective active_profile"
```

---

### Task 2: configClient — parse `envLocked` / `activeProfile`

**Files:**
- Modify: `desktop/src/lib/configClient.ts:14-19` (`ConfigPayload` interface) and `:42-50` (`parseConfig`)
- Test: `desktop/src/lib/configClient.test.ts` (update existing `parseConfig` expectations + add cases)

**Interfaces:**
- Consumes: the Task-1 `read()` payload (`env_locked`, `active_profile`).
- Produces: `ConfigPayload` now has `envLocked: boolean` and `activeProfile: string`; `parseConfig` populates them (defaults `false` / `"default"`). `toWriteBody` is unchanged (still picks only `base`/`profiles`/`local`).

- [ ] **Step 1: Update the failing tests**

In `desktop/src/lib/configClient.test.ts`, the existing `parseConfig` "defaults missing sections" assertion (lines 34–37) must learn the new fields. Replace it with:

```typescript
  it("defaults missing sections to empty objects (onboarding)", () => {
    const c = parseConfig({})!;
    expect(c).toEqual({
      base: {},
      profiles: {},
      local: {},
      secrets: {},
      envLocked: false,
      activeProfile: "default",
    });
  });

  it("parses env_locked and active_profile when present", () => {
    const c = parseConfig({ base: {}, env_locked: true, active_profile: "mobile" })!;
    expect(c.envLocked).toBe(true);
    expect(c.activeProfile).toBe("mobile");
  });

  it("coerces a non-string active_profile back to default", () => {
    const c = parseConfig({ base: {}, env_locked: "yes", active_profile: 7 })!;
    expect(c.envLocked).toBe(false); // only boolean true counts
    expect(c.activeProfile).toBe("default");
  });
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd desktop && npx vitest run src/lib/configClient.test.ts`
Expected: FAIL — the `toEqual` now expects `envLocked`/`activeProfile` the code does not yet produce; the two new cases fail (`undefined`).

- [ ] **Step 3: Implement the parse additions**

In `desktop/src/lib/configClient.ts`, extend the `ConfigPayload` interface (after the `secrets` field):

```typescript
/** The parsed `GET /config` payload. `secrets` carries only presence flags. */
export interface ConfigPayload {
  base: Record<string, unknown>;
  profiles: Record<string, Record<string, unknown>>;
  local: Record<string, unknown>;
  secrets: Record<string, SecretFlag>;
  /** True iff the sidecar runs under a `HERDECK_PROFILE` env lock (řez 4b uses it). */
  envLocked: boolean;
  /** The effective active profile name (env > local > base > "default"). */
  activeProfile: string;
}
```

Then update `parseConfig`'s `return` (line 49) to populate them:

```typescript
  const envLocked = v.env_locked === true;
  const activeProfile = typeof v.active_profile === "string" ? v.active_profile : "default";
  return { base: obj(v.base), profiles, local: obj(v.local), secrets, envLocked, activeProfile };
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd desktop && npx vitest run src/lib/configClient.test.ts`
Expected: PASS.

- [ ] **Step 5: Build (compile gate)**

Run: `cd desktop && npm run build`
Expected: build succeeds (vite compiles the Svelte + TS; no compile/type errors). (The new `ConfigPayload` fields have defaults in every `parseConfig` path, so existing consumers compile unchanged.)

- [ ] **Step 6: Commit**

```bash
git add desktop/src/lib/configClient.ts desktop/src/lib/configClient.test.ts
git commit -m "feat(config-editor): parse envLocked + activeProfile into ConfigPayload"
```

---

### Task 3: configClient — generic `getAt` / `setAt` / `removeAt` accessors

**Files:**
- Modify: `desktop/src/lib/configClient.ts` (append new helpers near the server mutators)
- Test: `desktop/src/lib/configClient.test.ts` (append a `describe`)

**Interfaces:**
- Consumes: `ConfigPayload`, the private `clone()` helper (already in the file).
- Produces three pure helpers used by every base-mode section (and the Hardware subsection):
  - `getAt(payload: ConfigPayload, root: "base" | "local", section: string, key: string): unknown` — reads `payload[root][section][key]`, or `undefined` when any level is missing/non-object.
  - `setAt(payload, root, section, key, value): ConfigPayload` — returns a NEW payload with `payload[root][section][key] = value` (input untouched; intermediate objects created as needed).
  - `removeAt(payload, root, section, key): ConfigPayload` — returns a NEW payload with that key deleted; an emptied section dict is left in place (no pruning — sections are sparse but harmless). Input untouched.

- [ ] **Step 1: Write the failing tests**

Append to `desktop/src/lib/configClient.test.ts`:

```typescript
import { getAt, setAt, removeAt } from "./configClient";

describe("getAt / setAt / removeAt", () => {
  it("getAt reads a nested base value or undefined", () => {
    const p = parseConfig({ base: { view: { management: "bottom_row" } } })!;
    expect(getAt(p, "base", "view", "management")).toBe("bottom_row");
    expect(getAt(p, "base", "view", "missing")).toBeUndefined();
    expect(getAt(p, "base", "deck", "grid")).toBeUndefined();
  });

  it("getAt reads the local hardware table", () => {
    const p = parseConfig({ local: { hardware: { brightness: 70 } } })!;
    expect(getAt(p, "local", "hardware", "brightness")).toBe(70);
  });

  it("setAt writes a new payload without touching the input", () => {
    const p = parseConfig({ base: { view: { management: "launcher_menu" } } })!;
    const next = setAt(p, "base", "view", "management", "bottom_row");
    expect(getAt(next, "base", "view", "management")).toBe("bottom_row");
    expect(getAt(p, "base", "view", "management")).toBe("launcher_menu"); // input untouched
  });

  it("setAt creates missing section + root objects", () => {
    const p = parseConfig({})!;
    const next = setAt(p, "local", "hardware", "brightness", 50);
    expect(getAt(next, "local", "hardware", "brightness")).toBe(50);
    expect(getAt(p, "local", "hardware", "brightness")).toBeUndefined();
  });

  it("setAt stores arrays and objects by value", () => {
    const p = parseConfig({})!;
    const next = setAt(p, "base", "view", "tile_fields", ["repo", "branch"]);
    expect(getAt(next, "base", "view", "tile_fields")).toEqual(["repo", "branch"]);
  });

  it("removeAt deletes the key on a copy", () => {
    const p = parseConfig({ base: { deck: { grid: "5x3", overview_order: ["a"] } } })!;
    const next = removeAt(p, "base", "deck", "grid");
    expect(getAt(next, "base", "deck", "grid")).toBeUndefined();
    expect(getAt(next, "base", "deck", "overview_order")).toEqual(["a"]); // sibling kept
    expect(getAt(p, "base", "deck", "grid")).toBe("5x3"); // input untouched
  });

  it("removeAt on an absent key is a harmless copy", () => {
    const p = parseConfig({ base: { deck: { grid: "5x3" } } })!;
    const next = removeAt(p, "base", "view", "management");
    expect(getAt(next, "base", "deck", "grid")).toBe("5x3");
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd desktop && npx vitest run src/lib/configClient.test.ts`
Expected: FAIL — `getAt`/`setAt`/`removeAt` are not exported.

- [ ] **Step 3: Implement the helpers**

Append to `desktop/src/lib/configClient.ts` (after the server mutators, before `commandTransport`):

```typescript
/** Editor root: the base config, or the machine-local config (`local.toml`). */
export type ConfigRoot = "base" | "local";

function asDict(v: unknown): Record<string, unknown> {
  return v != null && typeof v === "object" && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : {};
}

/** Read `payload[root][section][key]`, or undefined when any level is absent. */
export function getAt(
  payload: ConfigPayload,
  root: ConfigRoot,
  section: string,
  key: string,
): unknown {
  return asDict(asDict(payload[root])[section])[key];
}

/** NEW payload with `payload[root][section][key] = value`. Input untouched;
 *  intermediate root/section objects are created as needed. */
export function setAt(
  payload: ConfigPayload,
  root: ConfigRoot,
  section: string,
  key: string,
  value: unknown,
): ConfigPayload {
  const rootObj = clone(asDict(payload[root]));
  const sec = { ...asDict(rootObj[section]) };
  sec[key] = value;
  rootObj[section] = sec;
  return { ...payload, [root]: rootObj };
}

/** NEW payload with `payload[root][section][key]` deleted. The (possibly now
 *  empty) section dict is left in place. Input untouched. */
export function removeAt(
  payload: ConfigPayload,
  root: ConfigRoot,
  section: string,
  key: string,
): ConfigPayload {
  const rootObj = clone(asDict(payload[root]));
  const existing = rootObj[section];
  if (existing != null && typeof existing === "object" && !Array.isArray(existing)) {
    const sec = { ...(existing as Record<string, unknown>) };
    delete sec[key];
    rootObj[section] = sec;
  }
  return { ...payload, [root]: rootObj };
}
```

Note: `payload[root]` indexes `ConfigPayload` by `"base" | "local"`, both `Record<string, unknown>` — typechecks. The `{ ...payload, [root]: rootObj }` spread preserves `profiles`/`secrets`/`envLocked`/`activeProfile`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd desktop && npx vitest run src/lib/configClient.test.ts`
Expected: PASS.

- [ ] **Step 5: Build (compile gate)**

Run: `cd desktop && npm run build`
Expected: build succeeds (vite compiles the Svelte + TS; no compile/type errors).

- [ ] **Step 6: Commit**

```bash
git add desktop/src/lib/configClient.ts desktop/src/lib/configClient.test.ts
git commit -m "feat(config-editor): getAt/setAt/removeAt base+local accessors"
```

---

### Task 4: configClient — macros mutators + map-section serialization helpers

**Files:**
- Modify: `desktop/src/lib/configClient.ts` (append after the `getAt`/`setAt`/`removeAt` block)
- Test: `desktop/src/lib/configClient.test.ts` (append `describe`s)

**Interfaces:**
- Consumes: `ConfigPayload`, `clone`, `str`, `obj`, `asDict` (already in file).
- Produces macros helpers mirroring the server family — `base.macros` is `[{label, text}, ...]`:
  - `MacroRecord = { label: string; text: string }`
  - `macrosOf(payload): MacroRecord[]`
  - `addMacro(payload): ConfigPayload` (appends a blank `{label:"", text:""}`)
  - `removeMacro(payload, index): ConfigPayload`
  - `updateMacro(payload, index, field: keyof MacroRecord, value: string): ConfigPayload`
- AND the **pure map-section serialization core** used by the Start/Answer profile sections (the riskiest logic — extracted here so it is unit-tested, not buried in Svelte):
  - `StartProfileRow = { name: string; argv: string[] }`
  - `AnswerProfileRow = { name: string; approve: string[]; deny: string[]; stop: string[]; approve_always: string[] | null }` (`approve_always: null` = the key was ABSENT — backend falls back to `approve`).
  - `startProfileRows(payload): StartProfileRow[]` — read `base.start_profiles` (a `name → argv` map) as rows.
  - `answerProfileRows(payload): AnswerProfileRow[]` — read `base.answer_profiles` as rows, preserving `approve_always` ABSENCE as `null`.
  - `serializeNamedRows<R extends {name: string}, V>(rows, toValue): { duplicate: boolean; section: Record<string, V> | undefined }` — skip blank names; flag a duplicate name; return `undefined` for the section when no named rows remain (so the caller OMITS the key rather than writing `{}`).
  - `applyMapSection(payload, section, serialized): ConfigPayload | null` — write `base[section] = serialized` (or DELETE the key when `serialized` is `undefined`), returning the new payload, or `null` when the serialized section is unchanged (so the caller skips marking dirty).

- [ ] **Step 1: Write the failing tests**

Append to `desktop/src/lib/configClient.test.ts`:

```typescript
import { macrosOf, addMacro, removeMacro, updateMacro } from "./configClient";

describe("macros mutators", () => {
  const withMacros = () =>
    parseConfig({ base: { macros: [{ label: "go", text: "continue" }] } })!;

  it("macrosOf returns the base macros list or []", () => {
    expect(macrosOf(withMacros())).toEqual([{ label: "go", text: "continue" }]);
    expect(macrosOf(parseConfig({})!)).toEqual([]);
  });

  it("addMacro appends a blank record without touching the input", () => {
    const p = withMacros();
    const next = addMacro(p);
    expect(macrosOf(next)).toHaveLength(2);
    expect(macrosOf(next)[1]).toEqual({ label: "", text: "" });
    expect(macrosOf(p)).toHaveLength(1); // input untouched
  });

  it("updateMacro sets one field on a copy", () => {
    const p = withMacros();
    const next = updateMacro(p, 0, "text", "run tests");
    expect(macrosOf(next)[0]).toEqual({ label: "go", text: "run tests" });
    expect(macrosOf(p)[0].text).toBe("continue"); // input untouched
  });

  it("removeMacro drops the indexed record", () => {
    const p = addMacro(withMacros());
    const next = removeMacro(p, 0);
    expect(macrosOf(next)).toHaveLength(1);
    expect(macrosOf(next)[0]).toEqual({ label: "", text: "" });
  });

  it("removing the LAST macro omits base.macros (→ DEFAULT_MACROS, not [])", () => {
    const p = parseConfig({ base: { macros: [{ label: "go", text: "continue" }] } })!;
    const next = removeMacro(p, 0);
    expect(macrosOf(next)).toEqual([]);
    expect("macros" in (next.base as Record<string, unknown>)).toBe(false);
  });
});

import { startProfileRows, answerProfileRows, serializeNamedRows, applyMapSection, putList } from "./configClient";

describe("map-section serialization helpers", () => {
  it("putList writes a non-empty list but OMITS the key when empty (→ backend default)", () => {
    const p = parseConfig({ base: { view: { bottom_row: ["a", "b"] } } })!;
    const cleared = putList(p, "base", "view", "bottom_row", []);
    expect(getAt(cleared, "base", "view", "bottom_row")).toBeUndefined(); // omitted, not []
    const set = putList(p, "base", "view", "tile_fields", ["repo"]);
    expect(getAt(set, "base", "view", "tile_fields")).toEqual(["repo"]);
  });

  it("startProfileRows reads name→argv rows (or [])", () => {
    const p = parseConfig({ base: { start_profiles: { claude: ["claude"], codex: ["codex", "--x"] } } })!;
    expect(startProfileRows(p)).toEqual([
      { name: "claude", argv: ["claude"] },
      { name: "codex", argv: ["codex", "--x"] },
    ]);
    expect(startProfileRows(parseConfig({})!)).toEqual([]);
  });

  it("answerProfileRows preserves approve_always ABSENCE as null (not [])", () => {
    const p = parseConfig({
      base: {
        answer_profiles: {
          claude: { approve: ["1"], deny: ["esc"], stop: ["ctrl+c"], approve_always: ["2"] },
          codex: { approve: ["y"], deny: ["n"], stop: ["ctrl+c"] }, // no approve_always
        },
      },
    })!;
    const rows = answerProfileRows(p);
    expect(rows[0].approve_always).toEqual(["2"]);
    expect(rows[1].approve_always).toBeNull(); // absent — NOT []
  });

  it("serializeNamedRows skips blank names and omits an empty section (undefined)", () => {
    const r = serializeNamedRows([{ name: "", argv: ["x"] }], (e: { argv: string[] }) => e.argv);
    expect(r.duplicate).toBe(false);
    expect(r.section).toBeUndefined(); // no named rows → omit the key (never write {})
  });

  it("serializeNamedRows flags a duplicate name", () => {
    const r = serializeNamedRows(
      [{ name: "a", argv: ["1"] }, { name: "a", argv: ["2"] }],
      (e: { argv: string[] }) => e.argv,
    );
    expect(r.duplicate).toBe(true);
  });

  it("serializeNamedRows builds the map from named rows only", () => {
    const r = serializeNamedRows(
      [{ name: "claude", argv: ["claude"] }, { name: "", argv: [] }],
      (e: { argv: string[] }) => e.argv,
    );
    expect(r.section).toEqual({ claude: ["claude"] });
  });

  it("applyMapSection writes, deletes on undefined, and returns null on no-op", () => {
    const p = parseConfig({ base: { deck: { grid: "5x3" } } })!;
    expect(applyMapSection(p, "start_profiles", undefined)).toBeNull(); // absent → undefined = no change
    const withSec = applyMapSection(p, "start_profiles", { claude: ["claude"] })!;
    expect((withSec.base as Record<string, unknown>).start_profiles).toEqual({ claude: ["claude"] });
    const removed = applyMapSection(withSec, "start_profiles", undefined)!;
    expect("start_profiles" in (removed.base as Record<string, unknown>)).toBe(false);
    expect(applyMapSection(withSec, "start_profiles", { claude: ["claude"] })).toBeNull(); // unchanged
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd desktop && npx vitest run src/lib/configClient.test.ts`
Expected: FAIL — the macros mutators and the map-section helpers are not exported.

- [ ] **Step 3: Implement the mutators + map-section helpers**

Append to `desktop/src/lib/configClient.ts` (after the `getAt`/`setAt`/`removeAt` block):

```typescript
/** A base macro record as the editor edits it. */
export interface MacroRecord {
  label: string;
  text: string;
}

/** The base `macros` list as editable records (always an array). */
export function macrosOf(payload: ConfigPayload): MacroRecord[] {
  const raw = payload.base.macros;
  if (!Array.isArray(raw)) return [];
  return raw.map((m) => {
    const r = obj(m);
    return { label: str(r.label), text: str(r.text) };
  });
}

function withMacros(payload: ConfigPayload, macros: MacroRecord[]): ConfigPayload {
  // Absent `macros` means "use DEFAULT_MACROS"; an empty list would disable them. So when
  // the last macro is removed, OMIT the key (return to defaults) rather than write `[]`.
  const base = { ...clone(payload.base) };
  if (macros.length === 0) delete base.macros;
  else base.macros = macros;
  return { ...payload, base };
}

/** NEW payload with a blank macro appended. */
export function addMacro(payload: ConfigPayload): ConfigPayload {
  return withMacros(payload, [...macrosOf(payload), { label: "", text: "" }]);
}

/** NEW payload with macro `index` removed. */
export function removeMacro(payload: ConfigPayload, index: number): ConfigPayload {
  return withMacros(payload, macrosOf(payload).filter((_, i) => i !== index));
}

/** NEW payload with one field of macro `index` set. */
export function updateMacro(
  payload: ConfigPayload,
  index: number,
  field: keyof MacroRecord,
  value: string,
): ConfigPayload {
  const macros = macrosOf(payload).map((m, i) => (i === index ? { ...m, [field]: value } : m));
  return withMacros(payload, macros);
}

// --- map-section serialization core (used by Start/Answer profile sections) ---

/** A start-profile editor row. */
export interface StartProfileRow {
  name: string;
  argv: string[];
}

/** An answer-profile editor row. `approve_always: null` = the key was ABSENT, which the
 *  backend treats as "fall back to approve"; preserved so an unrelated edit never writes
 *  `[]` (which would mean "no approve-always keys" — a silent semantics change). */
export interface AnswerProfileRow {
  name: string;
  approve: string[];
  deny: string[];
  stop: string[];
  approve_always: string[] | null;
}

function strList(v: unknown): string[] {
  return Array.isArray(v) ? v.map(String) : [];
}

/** The base `start_profiles` map (`name → argv`) as editor rows. */
export function startProfileRows(payload: ConfigPayload): StartProfileRow[] {
  const sec = asDict((payload.base as Record<string, unknown>).start_profiles);
  return Object.entries(sec).map(([name, argv]) => ({ name, argv: strList(argv) }));
}

/** The base `answer_profiles` map as editor rows, preserving `approve_always` absence. */
export function answerProfileRows(payload: ConfigPayload): AnswerProfileRow[] {
  const sec = asDict((payload.base as Record<string, unknown>).answer_profiles);
  return Object.entries(sec).map(([name, raw]) => {
    const o = asDict(raw);
    return {
      name,
      approve: strList(o.approve),
      deny: strList(o.deny),
      stop: strList(o.stop),
      approve_always: "approve_always" in o ? strList(o.approve_always) : null,
    };
  });
}

/** Serialize named rows into a map section. Blank names are skipped; a repeated name sets
 *  `duplicate`; with no named rows the section is `undefined` so the caller OMITS the key
 *  (never writes `{}`, which would disable backend defaults). */
export function serializeNamedRows<R extends { name: string }, V>(
  rows: R[],
  toValue: (row: R) => V,
): { duplicate: boolean; section: Record<string, V> | undefined } {
  const named = rows.map((r) => r.name.trim()).filter((n) => n !== "");
  const duplicate = new Set(named).size !== named.length;
  const section: Record<string, V> = {};
  for (const r of rows) {
    const n = r.name.trim();
    if (n !== "") section[n] = toValue(r);
  }
  return { duplicate, section: Object.keys(section).length > 0 ? section : undefined };
}

/** NEW payload with `base[section]` set to `serialized` (or the key DELETED when
 *  `serialized` is `undefined` — absent means "use backend defaults", unlike `{}`), or
 *  `null` when the serialized section is unchanged (so the caller skips marking dirty). */
export function applyMapSection(
  payload: ConfigPayload,
  section: string,
  serialized: Record<string, unknown> | undefined,
): ConfigPayload | null {
  const current = (payload.base as Record<string, unknown>)[section];
  const currentSig = current === undefined ? "" : JSON.stringify(current);
  const nextSig = serialized === undefined ? "" : JSON.stringify(serialized);
  if (currentSig === nextSig) return null;
  const base = { ...(payload.base as Record<string, unknown>) };
  if (serialized === undefined) delete base[section];
  else base[section] = serialized;
  return { ...payload, base };
}

/** NEW payload writing `[root][section][key] = list`, or DELETING that key when `list` is
 *  empty. In this config an ABSENT list key means "use the backend default" (e.g.
 *  `DEFAULT_BOTTOM_ROW`, all servers for `overview_order`); writing an explicit `[]` would
 *  instead mean "none" and silently disable that default. So řez 4a maps an emptied list
 *  editor to "return to default" (omit the key). Authoring an INTENTIONAL explicit-empty
 *  list (e.g. `view.tile_primary = []` to switch a tile line off) is řez 4b's presence-aware
 *  override UX — out of scope here. */
export function putList(
  payload: ConfigPayload,
  root: ConfigRoot,
  section: string,
  key: string,
  list: string[],
): ConfigPayload {
  return list.length === 0
    ? removeAt(payload, root, section, key)
    : setAt(payload, root, section, key, list);
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd desktop && npx vitest run src/lib/configClient.test.ts`
Expected: PASS.

- [ ] **Step 5: Build (compile gate)**

Run: `cd desktop && npm run build`
Expected: build succeeds (vite compiles the Svelte + TS; no compile/type errors).

- [ ] **Step 6: Commit**

```bash
git add desktop/src/lib/configClient.ts desktop/src/lib/configClient.test.ts
git commit -m "feat(config-editor): macros mutators + tested map-section serialization helpers"
```

---

### Task 5: Field widgets — NumberField, BooleanField, SelectField

**Files:**
- Create: `desktop/src/lib/fields/NumberField.svelte`
- Create: `desktop/src/lib/fields/BooleanField.svelte`
- Create: `desktop/src/lib/fields/SelectField.svelte`

**Interfaces:**
- Consumes: nothing (pure presentational, mirror `TextField.svelte`).
- Produces three widgets the sections compose:
  - `NumberField` props `{ label: string; value: number | null; onchange: (v: number | null) => void; int?: boolean; step?: number }` — emits the parsed number (or `null` when blank/unparseable) on **commit (the DOM `change` event = blur/Enter)**, NOT on every keystroke, so a transient `0.` is not normalized to `0` mid-typing (a controlled number input re-parsed per keystroke makes decimals like `0.25` hard to enter).
  - `BooleanField` props `{ label: string; value: boolean; onchange: (v: boolean) => void }` — checkbox.
  - `SelectField` props `{ label: string; value: string; options: string[]; onchange: (v: string) => void }` — dropdown. If `value` is not in `options`, it is shown as a selected leading option so an unknown stored value is never silently dropped.

These are presentational; verification is `npm run build` (no component-test harness — see Global Constraints).

- [ ] **Step 1: Create NumberField.svelte**

```svelte
<script lang="ts">
  let {
    label,
    value,
    onchange,
    int = false,
    step = 1,
  }: {
    label: string;
    value: number | null;
    onchange: (v: number | null) => void;
    int?: boolean;
    step?: number;
  } = $props();

  // Commit on the DOM `change` event (blur/Enter), not per keystroke: a controlled
  // number input re-parsed on every keystroke turns "0." into "0", so decimals like
  // 0.25 are unenterable. Between commits the input holds its own raw text (the
  // `value` prop does not change, so Svelte never overwrites the focused field).
  // Parse with Number() (NOT parseInt/parseFloat, which truncate "1.9"→1 and accept
  // "1.2.3"→1.2): reject anything non-finite, and for int reject non-integers, → null.
  function emit(raw: string): void {
    const t = raw.trim();
    if (t === "") return onchange(null);
    const n = Number(t);
    if (!Number.isFinite(n) || (int && !Number.isInteger(n))) return onchange(null);
    onchange(n);
  }
</script>

<label class="field">
  <span>{label}</span>
  <input
    type="number"
    {step}
    value={value ?? ""}
    onchange={(e) => emit((e.target as HTMLInputElement).value)}
  />
</label>

<style>
  .field { display: grid; grid-template-columns: 120px 1fr; align-items: center; gap: 8px; margin: 4px 0; }
  .field span { color: #aaa; }
  input { background: #141417; border: 1px solid #2a2a30; color: inherit; padding: 4px 6px; border-radius: 4px; }
</style>
```

- [ ] **Step 2: Create BooleanField.svelte**

```svelte
<script lang="ts">
  let { label, value, onchange }:
    { label: string; value: boolean; onchange: (v: boolean) => void } = $props();
</script>

<label class="field">
  <input
    type="checkbox"
    checked={value}
    onchange={(e) => onchange((e.target as HTMLInputElement).checked)}
  />
  <span>{label}</span>
</label>

<style>
  .field { display: flex; align-items: center; gap: 8px; margin: 6px 0; cursor: pointer; }
  .field span { color: #ccc; }
</style>
```

- [ ] **Step 3: Create SelectField.svelte**

```svelte
<script lang="ts">
  let { label, value, options, onchange }:
    { label: string; value: string; options: string[]; onchange: (v: string) => void } = $props();

  // Surface an unknown stored value rather than silently snapping to options[0].
  const choices = $derived(options.includes(value) ? options : [value, ...options]);
</script>

<label class="field">
  <span>{label}</span>
  <select value={value} onchange={(e) => onchange((e.target as HTMLSelectElement).value)}>
    {#each choices as o}<option value={o}>{o}</option>{/each}
  </select>
</label>

<style>
  .field { display: grid; grid-template-columns: 120px 1fr; align-items: center; gap: 8px; margin: 4px 0; }
  .field span { color: #aaa; }
  select { background: #141417; border: 1px solid #2a2a30; color: inherit; padding: 4px 6px; border-radius: 4px; }
</style>
```

- [ ] **Step 4: Compile-smoke the widgets (Vitest) + build**

`npm run build` only compiles the reachable module graph, so these not-yet-imported widgets would NOT be checked by it. Add a Vitest compile-smoke that imports each (the vitest config has `svelte()` + jsdom, so an import compiles the `.svelte`). Create `desktop/src/lib/fields/widgets.smoke.test.ts`:

```typescript
import { describe, it, expect } from "vitest";
import NumberField from "./NumberField.svelte";
import BooleanField from "./BooleanField.svelte";
import SelectField from "./SelectField.svelte";

// Compile-smoke only: importing a .svelte compiles it (catches syntax/compile errors)
// without a render/interaction harness. New widgets are added here as they are created.
describe("field widget compile-smoke", () => {
  it("compiles the scalar widgets", () => {
    expect(NumberField).toBeTruthy();
    expect(BooleanField).toBeTruthy();
    expect(SelectField).toBeTruthy();
  });
});
```

Run: `cd desktop && npx vitest run src/lib/fields/widgets.smoke.test.ts && npm run build`
Expected: the smoke test passes (all three widgets compile) and the build succeeds.

- [ ] **Step 5: Commit**

```bash
git add desktop/src/lib/fields/NumberField.svelte desktop/src/lib/fields/BooleanField.svelte desktop/src/lib/fields/SelectField.svelte desktop/src/lib/fields/widgets.smoke.test.ts
git commit -m "feat(config-editor): NumberField, BooleanField, SelectField widgets"
```

---

### Task 6: Field widget — ListField (string-list editor)

**Files:**
- Create: `desktop/src/lib/fields/ListField.svelte`

**Interfaces:**
- Consumes: nothing.
- Produces `ListField` props `{ label: string; value: string[]; onchange: (v: string[]) => void }` — renders one text input per item with a remove `×` per row and a `+ přidat` button. Every edit emits the whole new array (the section stores it via `setAt`). Index keying (append/remove list, mirrors the justified choice in `ServersSection`).

- [ ] **Step 1: Create ListField.svelte**

```svelte
<script lang="ts">
  let { label, value, onchange }:
    { label: string; value: string[]; onchange: (v: string[]) => void } = $props();

  const items = $derived(Array.isArray(value) ? value : []);

  function setItem(i: number, v: string): void {
    onchange(items.map((x, j) => (j === i ? v : x)));
  }
  function add(): void {
    onchange([...items, ""]);
  }
  function remove(i: number): void {
    onchange(items.filter((_, j) => j !== i));
  }
</script>

<div class="listfield">
  <span class="label">{label}</span>
  <div class="rows">
    {#each items as item, i (i)}
      <div class="row">
        <input value={item} oninput={(e) => setItem(i, (e.target as HTMLInputElement).value)} />
        <button type="button" onclick={() => remove(i)}>×</button>
      </div>
    {/each}
    <button type="button" class="add" onclick={add}>+ přidat</button>
  </div>
</div>

<style>
  .listfield { display: grid; grid-template-columns: 120px 1fr; align-items: start; gap: 8px; margin: 6px 0; }
  .label { color: #aaa; padding-top: 4px; }
  .rows { display: flex; flex-direction: column; gap: 4px; }
  .row { display: flex; gap: 6px; }
  input { flex: 1; background: #141417; border: 1px solid #2a2a30; color: inherit; padding: 4px 6px; border-radius: 4px; }
  button { background: #1b1b1f; border: 1px solid #2a2a30; color: inherit; border-radius: 4px; padding: 4px 8px; cursor: pointer; }
  .row button { color: #e05050; }
  .add { align-self: flex-start; }
</style>
```

- [ ] **Step 2: Extend the compile-smoke with ListField + build**

Add ListField to `desktop/src/lib/fields/widgets.smoke.test.ts` (created in Task 5): add the import and an assertion.

```typescript
import ListField from "./ListField.svelte";
```

and inside the `describe`, add:

```typescript
  it("compiles ListField", () => {
    expect(ListField).toBeTruthy();
  });
```

Run: `cd desktop && npx vitest run src/lib/fields/widgets.smoke.test.ts && npm run build`
Expected: the smoke test passes (ListField compiles) and the build succeeds.

- [ ] **Step 3: Commit**

```bash
git add desktop/src/lib/fields/ListField.svelte desktop/src/lib/fields/widgets.smoke.test.ts
git commit -m "feat(config-editor): ListField string-list editor widget"
```

---

### Task 7: DeckSection (+ local Hardware subsection)

**Files:**
- Create: `desktop/src/lib/sections/DeckSection.svelte`
- Modify: `desktop/src/ConfigApp.svelte` (import + wire the `Deck` branch)

**Interfaces:**
- Consumes: `getAt`/`setAt`/`removeAt` (Task 3), `putList` (Task 4), `ListField` (Task 6), `TextField` (exists), `NumberField` (Task 5); `ConfigPayload`.
- Produces: a section editing base `[deck]` (`grid` text, `overview_order` string list) and a clearly-separated **Hardware (tento stroj)** block editing `local.local.*` + `local.hardware.*` (all hardware fields from the Global Constraints: deck, herdr_socket, web_bind, web_port, icons_dir, brightness, debounce, keep_alive_interval, tick_interval). Section prop contract matches `ServersSection`: `{ payload = $bindable(), onChange, onError }` (Deck does not use `onError`, but keeps the uniform signature so `ConfigApp` wires every section identically).

- [ ] **Step 1: Create DeckSection.svelte**

```svelte
<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import NumberField from "../fields/NumberField.svelte";
  import ListField from "../fields/ListField.svelte";
  import { getAt, setAt, removeAt, putList, type ConfigPayload } from "../configClient";

  let { payload = $bindable(), onChange }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  const grid = $derived((getAt(payload, "base", "deck", "grid") as string) ?? "");
  const overviewOrder = $derived((getAt(payload, "base", "deck", "overview_order") as string[]) ?? []);

  // Hardware (local.toml). [local] = deck kind / sockets / web bind; [hardware] = numeric tuning.
  const hwDeck = $derived((getAt(payload, "local", "local", "deck") as string) ?? "");
  const hwSocket = $derived((getAt(payload, "local", "local", "herdr_socket") as string) ?? "");
  const hwBind = $derived((getAt(payload, "local", "local", "web_bind") as string) ?? "");
  const hwIcons = $derived((getAt(payload, "local", "local", "icons_dir") as string) ?? "");
  const hwPort = $derived((getAt(payload, "local", "local", "web_port") as number | null) ?? null);
  const brightness = $derived((getAt(payload, "local", "hardware", "brightness") as number | null) ?? null);
  const debounce = $derived((getAt(payload, "local", "hardware", "debounce") as number | null) ?? null);
  const keepAlive = $derived((getAt(payload, "local", "hardware", "keep_alive_interval") as number | null) ?? null);
  const tick = $derived((getAt(payload, "local", "hardware", "tick_interval") as number | null) ?? null);

  function setBase(key: string, value: unknown): void {
    payload = setAt(payload, "base", "deck", key, value);
    onChange();
  }
  // overview_order is a list: empty → omit (backend default = all servers), not an empty selection.
  function setOverviewOrder(list: string[]): void {
    payload = putList(payload, "base", "deck", "overview_order", list);
    onChange();
  }
  // For optional local strings: blank clears the key (so we never write empty hardware paths).
  function setLocalStr(table: string, key: string, v: string): void {
    payload = v.trim() === "" ? removeAt(payload, "local", table, key) : setAt(payload, "local", table, key, v);
    onChange();
  }
  function setLocalNum(table: string, key: string, v: number | null): void {
    payload = v === null ? removeAt(payload, "local", table, key) : setAt(payload, "local", table, key, v);
    onChange();
  }
</script>

<h2>Deck</h2>
<TextField label="grid" value={grid} oninput={(v) => setBase("grid", v)} />
<ListField label="overview_order" value={overviewOrder} onchange={setOverviewOrder} />

<fieldset class="hw">
  <legend>Hardware (tento stroj — local.toml)</legend>
  <p class="hint">Platí jen pro tento počítač; nikdy se nepřenáší do profilů ani base configu.</p>
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

- [ ] **Step 2: Wire the Deck branch into ConfigApp.svelte**

In `desktop/src/ConfigApp.svelte`, add the import beside the existing `ServersSection` import (after line 6):

```svelte
  import DeckSection from "./lib/sections/DeckSection.svelte";
```

Then extend the section ladder. Replace the existing `{:else}` placeholder block (lines 120–122) with the Deck branch followed by the placeholder:

```svelte
      {:else if active === "Deck"}
        <DeckSection bind:payload onChange={markDirty} onError={(m) => (notice = m)} />
      {:else}
        <p class="hint">Sekce „{active}" — řez 4b.</p>
      {/if}
```

- [ ] **Step 3: Build + type-check**

Run: `cd desktop && npm run build`
Expected: 0 errors; build succeeds.

- [ ] **Step 4: Commit**

```bash
git add desktop/src/lib/sections/DeckSection.svelte desktop/src/ConfigApp.svelte
git commit -m "feat(config-editor): Deck section + local Hardware subsection"
```

---

### Task 8: ViewSection

**Files:**
- Create: `desktop/src/lib/sections/ViewSection.svelte`
- Modify: `desktop/src/ConfigApp.svelte` (import + wire the `View` branch)

**Interfaces:**
- Consumes: `getAt`/`setAt` (Task 3), `putList` (Task 4); `TextField`, `SelectField`, `BooleanField`, `ListField`; `ConfigPayload`.
- Produces: a section editing base `[view]` — `management` (SelectField `launcher_menu`/`bottom_row`), `agent_slots` (TextField — free text "max" or a number), `show_profile_on_panel` (BooleanField), and four token lists `bottom_row`, `tile_fields`, `tile_primary`, `tile_secondary` (ListField). Same `{ payload = $bindable(), onChange, onError }` contract.

- [ ] **Step 1: Create ViewSection.svelte**

```svelte
<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import SelectField from "../fields/SelectField.svelte";
  import BooleanField from "../fields/BooleanField.svelte";
  import ListField from "../fields/ListField.svelte";
  import { getAt, setAt, putList, type ConfigPayload } from "../configClient";

  let { payload = $bindable(), onChange }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  const MANAGEMENT = ["launcher_menu", "bottom_row"];

  const management = $derived((getAt(payload, "base", "view", "management") as string) ?? "launcher_menu");
  const agentSlots = $derived((getAt(payload, "base", "view", "agent_slots") as string) ?? "");
  const showProfile = $derived((getAt(payload, "base", "view", "show_profile_on_panel") as boolean) ?? false);
  const bottomRow = $derived((getAt(payload, "base", "view", "bottom_row") as string[]) ?? []);
  const tileFields = $derived((getAt(payload, "base", "view", "tile_fields") as string[]) ?? []);
  const tilePrimary = $derived((getAt(payload, "base", "view", "tile_primary") as string[]) ?? []);
  const tileSecondary = $derived((getAt(payload, "base", "view", "tile_secondary") as string[]) ?? []);

  // Scalars use setAt; lists use putList (empty list → omit key → backend default, see Task 4).
  function set(key: string, value: unknown): void {
    payload = setAt(payload, "base", "view", key, value);
    onChange();
  }
  function setList(key: string, list: string[]): void {
    payload = putList(payload, "base", "view", key, list);
    onChange();
  }
</script>

<h2>View</h2>
<SelectField label="management" value={management} options={MANAGEMENT} onchange={(v) => set("management", v)} />
<TextField label="agent_slots" value={agentSlots} oninput={(v) => set("agent_slots", v)} />
<BooleanField label="show_profile_on_panel" value={showProfile} onchange={(v) => set("show_profile_on_panel", v)} />
<ListField label="bottom_row" value={bottomRow} onchange={(v) => setList("bottom_row", v)} />
<ListField label="tile_fields" value={tileFields} onchange={(v) => setList("tile_fields", v)} />
<ListField label="tile_primary" value={tilePrimary} onchange={(v) => setList("tile_primary", v)} />
<ListField label="tile_secondary" value={tileSecondary} onchange={(v) => setList("tile_secondary", v)} />

<style>
  h2 { margin: 0 0 8px; }
</style>
```

- [ ] **Step 2: Wire the View branch into ConfigApp.svelte**

Add the import after the `DeckSection` import:

```svelte
  import ViewSection from "./lib/sections/ViewSection.svelte";
```

Add the branch before the `{:else}` placeholder:

```svelte
      {:else if active === "View"}
        <ViewSection bind:payload onChange={markDirty} onError={(m) => (notice = m)} />
```

- [ ] **Step 3: Build + type-check**

Run: `cd desktop && npm run build`
Expected: 0 errors; build succeeds.

- [ ] **Step 4: Commit**

```bash
git add desktop/src/lib/sections/ViewSection.svelte desktop/src/ConfigApp.svelte
git commit -m "feat(config-editor): View section"
```

---

### Task 9: ThemeSection

**Files:**
- Create: `desktop/src/lib/sections/ThemeSection.svelte`
- Modify: `desktop/src/ConfigApp.svelte`

**Interfaces:**
- Consumes: `getAt`/`setAt` (Task 3), `putList` (Task 4); `TextField` (exists), `ListField` (Task 6); `ConfigPayload`.
- Produces: a section editing base `[theme]` — `colors` shown as a **fixed set of the six known status colors** (`working`, `idle`, `blocked`, `done`, `unknown`, `offline`), each a `TextField` (a free-form map editor would let a blank key vanish on serialize; the status set is fixed, so fixed fields are both simpler and correct). A blank value clears that color key. `server_accents` is a `ListField`. Same contract.

- [ ] **Step 1: Create ThemeSection.svelte**

```svelte
<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import ListField from "../fields/ListField.svelte";
  import { getAt, setAt, putList, type ConfigPayload } from "../configClient";

  let { payload = $bindable(), onChange }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  // The known status keys (config.py DEFAULT_STATUS_COLORS). Fixed domain → fixed fields.
  const STATUS = ["working", "idle", "blocked", "done", "unknown", "offline"];

  function colorOf(key: string): string {
    return (getAt(payload, "base", "theme", "colors") as Record<string, unknown> | undefined)?.[key] as string ?? "";
  }
  const accents = $derived((getAt(payload, "base", "theme", "server_accents") as string[]) ?? []);

  function setColor(key: string, v: string): void {
    const cur = getAt(payload, "base", "theme", "colors");
    const colors: Record<string, unknown> =
      cur != null && typeof cur === "object" && !Array.isArray(cur) ? { ...(cur as Record<string, unknown>) } : {};
    if (v.trim() === "") delete colors[key];
    else colors[key] = v;
    payload = setAt(payload, "base", "theme", "colors", colors);
    onChange();
  }
  function setAccents(v: string[]): void {
    // list: empty → omit (backend default DEFAULT_SERVER_ACCENTS), not an explicit [].
    payload = putList(payload, "base", "theme", "server_accents", v);
    onChange();
  }
</script>

<h2>Theme</h2>
<fieldset class="colors">
  <legend>colors</legend>
  {#each STATUS as key (key)}
    <TextField label={key} value={colorOf(key)} oninput={(v) => setColor(key, v)} />
  {/each}
</fieldset>
<ListField label="server_accents" value={accents} onchange={setAccents} />

<style>
  h2 { margin: 0 0 8px; }
  .colors { border: 1px solid #2a2a30; border-radius: 6px; margin: 8px 0; padding: 8px 12px; }
  .colors legend { color: #ccc; }
</style>
```

Note: `colorOf` reads directly (not via `$derived`) so it stays a simple per-key getter; the section re-renders on `payload` reassignment because `payload` is a `$bindable` rune the template reads inside `colorOf`'s call sites.

- [ ] **Step 2: Wire the Theme branch into ConfigApp.svelte**

Add the import:

```svelte
  import ThemeSection from "./lib/sections/ThemeSection.svelte";
```

Add the branch before the `{:else}` placeholder:

```svelte
      {:else if active === "Theme"}
        <ThemeSection bind:payload onChange={markDirty} onError={(m) => (notice = m)} />
```

- [ ] **Step 3: Build + type-check**

Run: `cd desktop && npm run build`
Expected: 0 errors; build succeeds.

- [ ] **Step 4: Commit**

```bash
git add desktop/src/lib/sections/ThemeSection.svelte desktop/src/ConfigApp.svelte
git commit -m "feat(config-editor): Theme section (fixed status colors + server_accents)"
```

---

### Task 10: MacrosSection

**Files:**
- Create: `desktop/src/lib/sections/MacrosSection.svelte`
- Modify: `desktop/src/ConfigApp.svelte`

**Interfaces:**
- Consumes: `macrosOf`/`addMacro`/`removeMacro`/`updateMacro` (Task 4), `TextField`; `ConfigPayload`, `MacroRecord`.
- Produces: a list-of-records editor for base `[[macros]]` — `label` + `text` per row, add/remove. Mirrors `ServersSection` structure (index keying with the same justification comment). Same contract.

- [ ] **Step 1: Create MacrosSection.svelte**

```svelte
<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import {
    macrosOf, addMacro, removeMacro, updateMacro,
    type ConfigPayload, type MacroRecord,
  } from "../configClient";

  let { payload = $bindable(), onChange }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  const macros = $derived(macrosOf(payload));

  function set(i: number, field: keyof MacroRecord, v: string): void {
    payload = updateMacro(payload, i, field, v);
    onChange();
  }
  function add(): void {
    payload = addMacro(payload);
    onChange();
  }
  function remove(i: number): void {
    payload = removeMacro(payload, i);
    onChange();
  }
</script>

<h2>Macros</h2>
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

<style>
  h2 { margin: 0 0 8px; }
  fieldset { border: 1px solid #2a2a30; border-radius: 6px; margin: 8px 0; padding: 8px 12px; }
  legend { color: #ccc; } legend button { color: #e05050; background: none; border: 0; cursor: pointer; }
  button { background: #1b1b1f; border: 1px solid #2a2a30; color: inherit; border-radius: 4px; padding: 4px 8px; cursor: pointer; }
</style>
```

- [ ] **Step 2: Wire the Macros branch into ConfigApp.svelte**

Add the import:

```svelte
  import MacrosSection from "./lib/sections/MacrosSection.svelte";
```

Add the branch before the `{:else}` placeholder:

```svelte
      {:else if active === "Macros"}
        <MacrosSection bind:payload onChange={markDirty} onError={(m) => (notice = m)} />
```

- [ ] **Step 3: Build + type-check**

Run: `cd desktop && npm run build`
Expected: 0 errors; build succeeds.

- [ ] **Step 4: Commit**

```bash
git add desktop/src/lib/sections/MacrosSection.svelte desktop/src/ConfigApp.svelte
git commit -m "feat(config-editor): Macros section (label/text records)"
```

---

### Task 11: StartProfilesSection

**Files:**
- Create: `desktop/src/lib/sections/StartProfilesSection.svelte`
- Modify: `desktop/src/ConfigApp.svelte`

**Interfaces:**
- Consumes: `TextField`, `ListField`; and from configClient (Task 4) the tested map-serialization core — `startProfileRows`, `serializeNamedRows`, `applyMapSection` + `StartProfileRow`/`ConfigPayload`. A map-section cannot hold two entries with the same name, so the section keeps **editor rows in local `$state`** as the editing source of truth and delegates ALL serialization rules to the Task-4 helpers (it is a thin orchestrator).
- Produces: an editor for base `[start_profiles]` — a map of `name → argv string list`. Editor rows live in local `$state` (tolerating transient blank/duplicate names so a rename never drops a sibling); on every edit the rows serialize to the payload map (blank names skipped). **When no named entry remains, the `start_profiles` key is OMITTED, never written as `{}`** — per the absent≠empty rule, absent means "use `DEFAULT_START_PROFILES`" while an explicit `{}` would disable every launcher; authoring an INTENTIONAL empty map is řez 4b. The payload is touched (and dirty set) only when the serialized section actually changes, so clicking "add" (a blank row) neither wipes the defaults nor fakes a dirty state. **A duplicate name blocks the payload write** — the section surfaces an `onError` and keeps the last valid serialization, so a collapsed map can never reach the payload (and thus never reach Apply); the duplicate stays visible in `rows` until the user renames it. A new row starts blank (it persists in local state, just not yet in the payload). Rows re-seed from the payload ONLY when ConfigApp bumps the `reloadRev: number` prop (initial load / discard / post-Apply reload) — so the section's own writes never re-seed (no loop, no lost in-progress edit) and a discard that returns the same serialized payload still resets local-only/duplicate rows. Consumes the `reloadRev` prop. Uses `onError`. Same contract.

**Known limitation (accepted for řez 4a):** because `ConfigApp` renders sections through a `{#if active === ...}` ladder, switching to another section unmounts this component and resets its local rows. This is safe because the only rows in local state but NOT in the payload are (a) blank-named rows — and list editing (argv / answer keys) is **disabled until a name is entered**, so a blank-named row carries no user data to lose — and (b) the transient invalid state during a duplicate rename (where the payload still holds the last VALID map with both profiles intact, because a duplicate blocks the write). So navigating away never loses persistable config data — it only discards an incomplete/invalid draft, and returning re-seeds the valid payload state. Full draft persistence across navigation (keeping every section mounted) is deferred as disproportionate to this no-data-loss cosmetic gap.

- [ ] **Step 1: Create StartProfilesSection.svelte**

```svelte
<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import ListField from "../fields/ListField.svelte";
  import {
    startProfileRows, serializeNamedRows, applyMapSection,
    type ConfigPayload, type StartProfileRow,
  } from "../configClient";

  let { payload = $bindable(), onChange, onError, reloadRev }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; reloadRev: number } = $props();

  // Local editor rows are the source of truth WHILE editing — they may hold blank or
  // duplicate names that a map cannot, so a rename never collapses a sibling. Re-seed
  // ONLY when ConfigApp bumps `reloadRev` (an explicit load/discard/Apply-reload signal):
  // a same-content discard still resets local-only rows that never reached the payload.
  // Our own commits don't bump reloadRev, so they never re-seed (no loop, no lost edit).
  let rows = $state<StartProfileRow[]>(startProfileRows(payload));
  let seenRev = $state(reloadRev);

  $effect(() => {
    if (reloadRev !== seenRev) {
      seenRev = reloadRev;
      rows = startProfileRows(payload);
    }
  });

  // All map-serialization rules (skip blank, block duplicate, omit empty section, no-op
  // detection) live in the tested configClient helpers; this stays a thin orchestrator.
  function commit(next: StartProfileRow[]): void {
    rows = next; // local rows always reflect the edit, so the user sees + can fix a clash
    const { duplicate, section } = serializeNamedRows(next, (r) => r.argv);
    if (duplicate) {
      onError("duplicitní jméno start profilu — neuloží se, dokud nepřejmenuješ");
      return;
    }
    const updated = applyMapSection(payload, "start_profiles", section);
    if (updated === null) return; // unchanged serialized section → no dirty
    payload = updated;
    onChange();
  }

  function rename(i: number, name: string): void { commit(rows.map((r, j) => (j === i ? { ...r, name } : r))); }
  function setArgv(i: number, argv: string[]): void { commit(rows.map((r, j) => (j === i ? { ...r, argv } : r))); }
  function add(): void { commit([...rows, { name: "", argv: [] }]); }
  function remove(i: number): void { commit(rows.filter((_, j) => j !== i)); }
</script>

<h2>Start profiles</h2>
<p class="hint">Spouštěcí příkaz (argv) pro každý typ agenta startovaného z decku.</p>
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

<style>
  h2 { margin: 0 0 8px; }
  .hint { color: #888; margin: 0 0 8px; }
  fieldset { border: 1px solid #2a2a30; border-radius: 6px; margin: 8px 0; padding: 8px 12px; }
  legend { color: #ccc; } legend button { color: #e05050; background: none; border: 0; cursor: pointer; }
  button { background: #1b1b1f; border: 1px solid #2a2a30; color: inherit; border-radius: 4px; padding: 4px 8px; cursor: pointer; }
</style>
```

- [ ] **Step 2: Add the reloadRev signal to ConfigApp + wire the Start profiles branch**

Add the import:

```svelte
  import StartProfilesSection from "./lib/sections/StartProfilesSection.svelte";
```

Add a reload-revision counter beside the other `$state` declarations (after `let notice = $state("");`). It bumps on every (re)load so the map sections (Start profiles, Answer profiles) re-seed their local editor rows — covering a discard that returns the SAME serialized payload but must still drop local-only/duplicate rows:

```svelte
  let reloadRev = $state(0); // bumps on every load(); map sections re-seed local rows on change
```

In the existing `load()` (still the řez-3 version at this point), bump it on a successful read — add `reloadRev += 1;` right after `errors = [];`:

```svelte
  async function load(): Promise<void> {
    try {
      payload = parseConfig(await cfg.read());
      dirty = false;
      errors = [];
      reloadRev += 1;
    } catch {
      payload = null; // sidecar not ready / no config -> onboarding handled in this řez
    }
  }
```

Add the branch before the `{:else}` placeholder (note the sidebar label has a space), passing `reloadRev`:

```svelte
      {:else if active === "Start profiles"}
        <StartProfilesSection bind:payload {reloadRev} onChange={markDirty} onError={(m) => (notice = m)} />
```

- [ ] **Step 3: Build + type-check**

Run: `cd desktop && npm run build`
Expected: 0 errors; build succeeds.

- [ ] **Step 4: Commit**

```bash
git add desktop/src/lib/sections/StartProfilesSection.svelte desktop/src/ConfigApp.svelte
git commit -m "feat(config-editor): Start profiles section (name -> argv map)"
```

---

### Task 12: NotificationsSection (incl. telegram secret)

**Files:**
- Create: `desktop/src/lib/sections/NotificationsSection.svelte`
- Modify: `desktop/src/ConfigApp.svelte`

**Interfaces:**
- Consumes: `getAt`/`setAt`/`removeAt` (Task 3), `putList` (Task 4), `secretFlag` + `commandTransport` (exist), `BooleanField`, `ListField`, `TextField`, `TokenSecretField`; `ConfigPayload`.
- Produces: an editor for base `[notifications]` — `enabled` (BooleanField), `sound` (BooleanField), `on` (ListField of status names), `backends` (ListField), and a nested **telegram** block: `token_env` (TokenSecretField → keychain via `config_secret_set`/`clear`, exactly like Servers) + `chat_id` (TextField). The telegram block is written as `notifications.telegram` with **only the non-blank sub-fields** — a blank `token_env` is never written as `""` (the backend token collector would treat `""` as an env-var name and crash validation outside the normal error path), and when both fields are blank the whole telegram key is removed (`removeAt`) so no empty table is left. A partially-filled telegram (only one field) is kept so input isn't lost; the backend ignores it (with a warning) until both are set. Uses `onError` for failed secret ops (mirrors Servers). Same contract.

- [ ] **Step 1: Create NotificationsSection.svelte**

```svelte
<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import BooleanField from "../fields/BooleanField.svelte";
  import ListField from "../fields/ListField.svelte";
  import TextField from "../fields/TextField.svelte";
  import TokenSecretField from "../fields/TokenSecretField.svelte";
  import {
    commandTransport as cfgTransport, getAt, setAt, removeAt, putList, secretFlag, type ConfigPayload,
  } from "../configClient";

  let { payload = $bindable(), onChange, onError }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  const cfg = cfgTransport((cmd, args) => invoke(cmd, args));

  const enabled = $derived((getAt(payload, "base", "notifications", "enabled") as boolean) ?? false);
  const sound = $derived((getAt(payload, "base", "notifications", "sound") as boolean) ?? true);
  const on = $derived((getAt(payload, "base", "notifications", "on") as string[]) ?? []);
  const backends = $derived((getAt(payload, "base", "notifications", "backends") as string[]) ?? []);

  const telegram = $derived(((): { token_env: string; chat_id: string } => {
    const v = getAt(payload, "base", "notifications", "telegram");
    const t = v != null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
    return { token_env: String(t.token_env ?? ""), chat_id: String(t.chat_id ?? "") };
  })());

  function set(key: string, value: unknown): void {
    payload = setAt(payload, "base", "notifications", key, value);
    onChange();
  }
  // `on`/`backends` are lists: empty → omit (backend defaults ["blocked"]/["macos"]), not [].
  function setList(key: string, list: string[]): void {
    payload = putList(payload, "base", "notifications", key, list);
    onChange();
  }
  function setTelegram(field: "token_env" | "chat_id", v: string): void {
    const next = { ...telegram, [field]: v };
    if (next.token_env.trim() === "" && next.chat_id.trim() === "") {
      // Both cleared → drop the table entirely (no empty [telegram]).
      payload = removeAt(payload, "base", "notifications", "telegram");
    } else {
      // Omit a BLANK sub-field rather than writing `token_env = ""`: the backend token
      // collector would treat "" as an env-var name and crash validation outside the
      // normal error path. A telegram table is only fully valid with BOTH fields; a
      // partial one is ignored by the backend (with a warning) but kept here so the
      // half-entered value (e.g. chat_id typed before token_env) is not lost.
      const tg: Record<string, string> = {};
      if (next.token_env.trim() !== "") tg.token_env = next.token_env;
      if (next.chat_id.trim() !== "") tg.chat_id = next.chat_id;
      payload = setAt(payload, "base", "notifications", "telegram", tg);
    }
    onChange();
  }
  async function setSecret(name: string, value: string): Promise<void> {
    const code = await cfg.setSecret(name, value);
    if (code === 204) {
      payload = { ...payload, secrets: { ...payload.secrets, [name]: { set: true, source: "keychain" } } };
    } else {
      onError(`uložení tokenu '${name}' selhalo (HTTP ${code})`);
    }
  }
  async function clearSecret(name: string): Promise<void> {
    const code = await cfg.clearSecret(name);
    if (code === 204) {
      payload = { ...payload, secrets: { ...payload.secrets, [name]: { set: false, source: null } } };
    } else {
      onError(`smazání tokenu '${name}' selhalo (HTTP ${code})`);
    }
  }
</script>

<h2>Notifications</h2>
<BooleanField label="enabled" value={enabled} onchange={(v) => set("enabled", v)} />
<BooleanField label="sound" value={sound} onchange={(v) => set("sound", v)} />
<ListField label="on" value={on} onchange={(v) => setList("on", v)} />
<ListField label="backends" value={backends} onchange={(v) => setList("backends", v)} />

<fieldset class="tg">
  <legend>Telegram</legend>
  <TokenSecretField
    label="token"
    value={telegram.token_env}
    flag={secretFlag(payload, telegram.token_env)}
    oninput={(v) => setTelegram("token_env", v)}
    onset={(val) => setSecret(telegram.token_env, val)}
    onclear={() => clearSecret(telegram.token_env)}
  />
  <TextField label="chat_id" value={telegram.chat_id} oninput={(v) => setTelegram("chat_id", v)} />
</fieldset>

<style>
  h2 { margin: 0 0 8px; }
  .tg { border: 1px solid #2a2a30; border-radius: 6px; margin: 12px 0; padding: 8px 12px; }
  .tg legend { color: #ccc; }
</style>
```

- [ ] **Step 2: Wire the Notifications branch into ConfigApp.svelte**

Add the import:

```svelte
  import NotificationsSection from "./lib/sections/NotificationsSection.svelte";
```

Add the branch before the `{:else}` placeholder:

```svelte
      {:else if active === "Notifications"}
        <NotificationsSection bind:payload onChange={markDirty} onError={(m) => (notice = m)} />
```

- [ ] **Step 3: Build + type-check**

Run: `cd desktop && npm run build`
Expected: 0 errors; build succeeds.

- [ ] **Step 4: Commit**

```bash
git add desktop/src/lib/sections/NotificationsSection.svelte desktop/src/ConfigApp.svelte
git commit -m "feat(config-editor): Notifications section (+ telegram token_env secret)"
```

---

### Task 13: SafetySection

**Files:**
- Create: `desktop/src/lib/sections/SafetySection.svelte`
- Modify: `desktop/src/ConfigApp.svelte`

**Interfaces:**
- Consumes: `getAt`/`setAt` (Task 3), `putList` (Task 4), `BooleanField`, `ListField`; `ConfigPayload`.
- Produces: an editor for base `[safety]` — `approve_always` (BooleanField) + `require_confirm_for` (ListField). Same contract.

- [ ] **Step 1: Create SafetySection.svelte**

```svelte
<script lang="ts">
  import BooleanField from "../fields/BooleanField.svelte";
  import ListField from "../fields/ListField.svelte";
  import { getAt, setAt, putList, type ConfigPayload } from "../configClient";

  let { payload = $bindable(), onChange }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  const approveAlways = $derived((getAt(payload, "base", "safety", "approve_always") as boolean) ?? true);
  const requireConfirmFor = $derived((getAt(payload, "base", "safety", "require_confirm_for") as string[]) ?? []);

  function set(key: string, value: unknown): void {
    payload = setAt(payload, "base", "safety", key, value);
    onChange();
  }
  function setRequireConfirmFor(list: string[]): void {
    payload = putList(payload, "base", "safety", "require_confirm_for", list);
    onChange();
  }
</script>

<h2>Safety</h2>
<BooleanField label="approve_always" value={approveAlways} onchange={(v) => set("approve_always", v)} />
<ListField label="require_confirm_for" value={requireConfirmFor} onchange={setRequireConfirmFor} />

<style>
  h2 { margin: 0 0 8px; }
</style>
```

- [ ] **Step 2: Wire the Safety branch into ConfigApp.svelte**

Add the import:

```svelte
  import SafetySection from "./lib/sections/SafetySection.svelte";
```

Add the branch before the `{:else}` placeholder:

```svelte
      {:else if active === "Safety"}
        <SafetySection bind:payload onChange={markDirty} onError={(m) => (notice = m)} />
```

- [ ] **Step 3: Build + type-check**

Run: `cd desktop && npm run build`
Expected: 0 errors; build succeeds.

- [ ] **Step 4: Commit**

```bash
git add desktop/src/lib/sections/SafetySection.svelte desktop/src/ConfigApp.svelte
git commit -m "feat(config-editor): Safety section"
```

---

### Task 14: AnswerProfilesSection

**Files:**
- Create: `desktop/src/lib/sections/AnswerProfilesSection.svelte`
- Modify: `desktop/src/ConfigApp.svelte`

**Interfaces:**
- Consumes: `TextField`, `ListField`; and from configClient (Task 4) `answerProfileRows`, `serializeNamedRows`, `applyMapSection` + `AnswerProfileRow`/`ConfigPayload`. Same map-collapse hazard as Start profiles, so editor rows live in local `$state` and all serialization (incl. the `approve_always`-absence preservation) is delegated to the tested Task-4 helpers.
- Produces: an editor for base `[answer_profiles]` — a map of `name → {approve, deny, stop, approve_always}` where each value is a string list (key tokens). `approve_always` is **nullable**: when the key was absent it stays `null` and is OMITTED on serialize (the backend `_parse_profile` falls back `approve_always → approve` only when the key is absent — writing `[]` would instead mean "no approve-always keys", a silent semantics change on an unrelated edit). The argv/key `ListField`s are disabled until a name is entered (so a blank-named row carries no editable data). Editor rows live in local `$state` (transient blank/duplicate names tolerated); on every edit the rows serialize to the payload map (blank names skipped). An empty section is OMITTED (not written as `{}`) and dirty is set only on a real serialized change — same guards as StartProfilesSection. **A duplicate name blocks the payload write** (same guarantee as StartProfilesSection — last valid map kept, collapse never reaches Apply, duplicate stays visible until renamed). A new row starts blank. Rows re-seed from the payload only when ConfigApp bumps the `reloadRev: number` prop, via the same mechanism as StartProfilesSection. Consumes the `reloadRev` prop. Uses `onError`. Same contract.

- [ ] **Step 1: Create AnswerProfilesSection.svelte**

```svelte
<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import ListField from "../fields/ListField.svelte";
  import {
    answerProfileRows, serializeNamedRows, applyMapSection,
    type ConfigPayload, type AnswerProfileRow,
  } from "../configClient";

  let { payload = $bindable(), onChange, onError, reloadRev }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void; reloadRev: number } = $props();

  const KEYS = ["approve", "deny", "stop", "approve_always"] as const;

  // Local editor rows (source of truth while editing); re-seeded only when ConfigApp bumps
  // `reloadRev` (load/discard/Apply-reload) — same pattern as StartProfilesSection.
  let rows = $state<AnswerProfileRow[]>(answerProfileRows(payload));
  let seenRev = $state(reloadRev);

  $effect(() => {
    if (reloadRev !== seenRev) {
      seenRev = reloadRev;
      rows = answerProfileRows(payload);
    }
  });

  // Serialization rules live in the tested configClient helpers; approve_always is omitted
  // when null (absent → backend falls back to approve), so an unrelated edit never writes [].
  function commit(next: AnswerProfileRow[]): void {
    rows = next; // local rows always reflect the edit, so the user sees + can fix a clash
    const { duplicate, section } = serializeNamedRows(next, (r) => {
      const prof: Record<string, string[]> = { approve: r.approve, deny: r.deny, stop: r.stop };
      if (r.approve_always !== null) prof.approve_always = r.approve_always;
      return prof;
    });
    if (duplicate) {
      onError("duplicitní jméno answer profilu — neuloží se, dokud nepřejmenuješ");
      return;
    }
    const updated = applyMapSection(payload, "answer_profiles", section);
    if (updated === null) return; // unchanged serialized section → no dirty
    payload = updated;
    onChange();
  }

  function rename(i: number, name: string): void { commit(rows.map((r, j) => (j === i ? { ...r, name } : r))); }
  function setList(i: number, key: (typeof KEYS)[number], v: string[]): void {
    commit(rows.map((r, j) => (j === i ? { ...r, [key]: v } : r)));
  }
  function add(): void {
    commit([...rows, { name: "", approve: [], deny: [], stop: [], approve_always: null }]);
  }
  function remove(i: number): void { commit(rows.filter((_, j) => j !== i)); }
</script>

<h2>Answer profiles</h2>
<p class="hint">Klávesy posílané agentovi pro approve / deny / stop podle typu agenta.</p>
{#each rows as e, i (i)}
  <fieldset>
    <legend>{e.name || "(nový profil)"} <button type="button" onclick={() => remove(i)}>×</button></legend>
    <TextField label="name" value={e.name} oninput={(v) => rename(i, v)} />
    {#if e.name.trim() !== ""}
      {#each KEYS as k}
        <ListField label={k} value={e[k] ?? []} onchange={(v) => setList(i, k, v)} />
      {/each}
    {:else}
      <p class="hint">Zadej jméno profilu pro úpravu kláves.</p>
    {/if}
  </fieldset>
{/each}
<button type="button" onclick={add}>+ přidat profil</button>

<style>
  h2 { margin: 0 0 8px; }
  .hint { color: #888; margin: 0 0 8px; }
  fieldset { border: 1px solid #2a2a30; border-radius: 6px; margin: 8px 0; padding: 8px 12px; }
  legend { color: #ccc; } legend button { color: #e05050; background: none; border: 0; cursor: pointer; }
  button { background: #1b1b1f; border: 1px solid #2a2a30; color: inherit; border-radius: 4px; padding: 4px 8px; cursor: pointer; }
</style>
```

- [ ] **Step 2: Wire the Answer profiles branch into ConfigApp.svelte**

Add the import:

```svelte
  import AnswerProfilesSection from "./lib/sections/AnswerProfilesSection.svelte";
```

Add the branch before the `{:else}` placeholder (passing the `reloadRev` signal added in Task 11):

```svelte
      {:else if active === "Answer profiles"}
        <AnswerProfilesSection bind:payload {reloadRev} onChange={markDirty} onError={(m) => (notice = m)} />
```

- [ ] **Step 3: Build + type-check**

Run: `cd desktop && npm run build`
Expected: 0 errors; build succeeds.

- [ ] **Step 4: Commit**

```bash
git add desktop/src/lib/sections/AnswerProfilesSection.svelte desktop/src/ConfigApp.svelte
git commit -m "feat(config-editor): Answer profiles section"
```

---

### Task 15: Onboarding empty-state + load error/empty distinction

**Files:**
- Modify: `desktop/src/ConfigApp.svelte` (`load()`, the form area, a tiny banner)

**Interfaces:**
- Consumes: everything wired above.
- Produces: (1) `load()` no longer conflates a transport/sidecar error with a real "no config" reply — a thrown error sets a visible `notice` instead of silently nulling the payload; a successful read that simply has no config yields an empty (but non-null) payload, so the section forms render with defaults. (2) When the active section is `Servers` and there are no servers, an inline onboarding hint nudges the user to add the first server. This closes the řez-3 deferred "bare-catch conflates no-config with real error" item.

- [ ] **Step 1: Make load() distinguish error from empty**

In `desktop/src/ConfigApp.svelte`, replace the `load()` function (lines 34–42) with:

```svelte
  async function load(): Promise<void> {
    try {
      const fresh = parseConfig(await cfg.read());
      if (fresh == null) {
        // A 200 that is not an object should not wipe the editor; surface it.
        notice = "neočekávaná odpověď configu ze sidecaru";
        return;
      }
      payload = fresh;
      dirty = false;
      errors = [];
      notice = "";
      reloadRev += 1; // re-seed map sections' local rows (keep the bump from Task 11)
    } catch {
      // Transport/sidecar error (404 no config service, sidecar down, reload failed).
      // ALWAYS surface it; keep any in-memory payload — never silently null a loaded
      // config, and never swallow a failed discard/reload after a payload exists.
      notice = payload == null
        ? "sidecar zatím neběží — zkouším znovu…"
        : "obnovení configu ze sidecaru selhalo (neuložené změny zůstávají)";
    }
  }
```

- [ ] **Step 2: Add the onboarding hint in the Servers branch area**

In the `<section class="form">` block, replace the existing `Servers` branch:

```svelte
      {:else if active === "Servers"}
        <ServersSection bind:payload onChange={markDirty} onError={(m) => (notice = m)} />
```

with:

```svelte
      {:else if active === "Servers"}
        {#if (payload.base.servers == null || (payload.base.servers as unknown[]).length === 0)}
          <p class="hint">Zatím žádný server. Přidej první a klikni Apply pro vytvoření configu.</p>
        {/if}
        <ServersSection bind:payload onChange={markDirty} onError={(m) => (notice = m)} />
```

- [ ] **Step 3: Build + type-check**

Run: `cd desktop && npm run build`
Expected: 0 errors; build succeeds.

- [ ] **Step 4: Run the full desktop test suite**

Run: `cd desktop && npx vitest run`
Expected: all pure-logic tests pass (configClient + deckClient + sidecar).

- [ ] **Step 5: Commit**

```bash
git add desktop/src/ConfigApp.svelte
git commit -m "feat(config-editor): onboarding empty-state + load error/empty distinction"
```

---

## Self-Review

**1. Spec coverage** (against `2026-06-25-config-editor-frontend-design.md`, the řez-4 portion):
- "zbylých 9 sekcí" — řez 4a covers 8 (Deck, View, Theme, Macros, Start profiles, Notifications, Safety, Answer profiles); the 9th, **Profiles**, is profile-overlay machinery → explicitly řez 4b. ✅ (documented split)
- "všechny field widgety" — NumberField/BooleanField/SelectField/ListField (Tasks 5–6) + reused TextField/TokenSecretField. Theme colors use fixed `TextField`s (no map widget needed); `OverrideField` is overlay-only → řez 4b. ✅
- "secrets UX (set/clear)" — reused in Servers (řez 3) + Notifications telegram (Task 12). ✅
- "onboarding" — Task 15. ✅
- `env_locked` additive `read()` field — Task 1 (parsed Task 2). ✅
- "OverrideField overlay UX", "profile create/delete + active-switch wiring", "klik-to-jump preview", "error bannery/toasty" — **all řez 4b** (out of scope here; stated in Global Constraints + the plan goal). ✅
- Hardware local-only — Task 7 Hardware subsection writing `local.local`/`local.hardware` (all nine fields), never base/profile `[deck]`. ✅

**2. Placeholder scan:** No "TBD/TODO/handle edge cases". Tasks 11 & 14 keep editor rows in local `$state` (the roborev fix for the blank-on-add and duplicate-rename map-collapse findings): rows tolerate transient blank/duplicate names, but a duplicate name **blocks the payload write** (last valid map kept) so a collapsed map can never reach Apply — no silent data loss; rows re-seed from the payload only when ConfigApp bumps the explicit `reloadRev` signal (load/discard/Apply-reload), which also resets local-only rows after a same-content discard. The map-serialization rules themselves (skip blank, block duplicate, omit empty section so backend defaults survive, no-op detection, `approve_always`-absence preservation) are extracted into pure configClient helpers (Task 4) with direct Vitest coverage — the sections are thin orchestrators, satisfying "pure logic = TDD". The absent≠empty rule is enforced uniformly: scalar-section list fields go through `putList` (empty list → OMIT key → backend default), and `withMacros` omits `base.macros` when the last row is removed — so editing never silently disables `DEFAULT_BOTTOM_ROW`/`DEFAULT_MACROS`/`DEFAULT_START_PROFILES`/etc.; intentional explicit-empty authoring is deferred to řez 4b. `NumberField` commits on blur (DOM `change`) with `Number()`-based parsing (rejecting `1.9` for int fields) so decimals are enterable and lossless. Every component's full code is inline; no "similar to Task N".

**3. Type consistency:**
- `ConfigPayload` gains `envLocked`/`activeProfile` in Task 2; every later `parseConfig` consumer compiles because both have defaults. `toWriteBody` still drops them (it only spreads base/profiles/local). ✅
- `getAt(payload, root, section, key)` / `setAt(...)` / `removeAt(...)` signatures (Task 3) are used identically in Tasks 7–13. ✅
- Section prop contract `{ payload = $bindable(), onChange, onError }` is uniform across all sections and matches how `ConfigApp` wires each branch (`bind:payload onChange={markDirty} onError={(m) => (notice = m)}`). Sections that ignore `onError` still declare it for a uniform call site; Start/Answer profiles + Notifications actively use it. The two map sections (Start/Answer profiles) additionally take `reloadRev: number`, wired as `{reloadRev}` in ConfigApp; `reloadRev` is declared once (Task 11) and bumped in `load()` (preserved by the Task 15 rewrite). ✅
- Widget prop names: `oninput` (TextField), `onchange` (NumberField — commit-on-blur — plus Boolean/Select/List) — used consistently in each section (DeckSection wires every NumberField via `onchange`). ✅
- `MacroRecord` (Task 4) used in MacrosSection (Task 10). ✅
- Map-section row safety: Theme uses fixed status keys (no blank/dup possible); Start/Answer profiles keep rows in local `$state` so a blank or colliding name never silently drops a sibling. ✅

## Execution Handoff

After implementation, this řez is followed by **řez 4b** (a separate plan): the Profiles section + profile switcher wiring (`config_set_active` + `env_locked` disabled state), `OverrideField` overlay mode across the base sections, klik-to-jump preview, and error-banner/toast polish.
</content>
