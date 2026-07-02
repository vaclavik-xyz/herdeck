import { describe, it, expect } from "vitest";
import {
  parseConfig,
  parseValidate,
  commandTransport,
  toWriteBody,
  inheritedValue,
  setOverride,
  clearOverride,
  secretFlag,
  profileExtends,
  setProfileExtends,
  profileServers,
  setProfileServers,
  listFieldState,
  setListField,
  inheritedFor,
  inheritedForPath,
  overrideValue,
  overrideValuePath,
  overrideState,
  setOverridePath,
  clearOverridePath,
  mergeSection,
  inheritedSection,
  inheritedStartProfiles,
  inheritedMacros,
  inheritedAnswerProfiles,
  DEFAULT_START_PROFILES,
  DEFAULT_MACROS,
  overrideStatePath,
  macroRecords,
  mapSectionState,
  setMapSectionState,
  profileServersState,
  setProfileServersExplicit,
  clearProfileServers,
  DEFAULT_TOGGLE_DECK_HOTKEY,
  toggleDeckHotkey,
  setToggleDeckHotkey,
  WINDOW_MODES,
  DEFAULT_WINDOW_MODE,
  windowMode,
  setWindowMode,
  type ConfigPayload,
  errorCountLabel,
  effectiveLanguage,
  isStaleRevisionError,
} from "./configClient";

function rawConfig(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    base: { servers: [{ id: "local", url: "ws://x", token_env: "TOK" }], deck: { grid: "5x3" } },
    profiles: { mobile: { view: { management: "bottom_row" } } },
    local: { active_profile: "mobile" },
    secrets: { TOK: { set: true, source: "env" } },
    ...over,
  };
}

describe("parseConfig", () => {
  it("shapes a well-formed /config payload", () => {
    const c = parseConfig(rawConfig())!;
    expect(c).not.toBeNull();
    expect(c.base.deck).toEqual({ grid: "5x3" });
    expect(c.profiles.mobile.view).toEqual({ management: "bottom_row" });
    expect(c.local.active_profile).toBe("mobile");
    expect(c.secrets.TOK).toEqual({ set: true, source: "env" });
  });

  it("defaults missing sections to empty objects (onboarding)", () => {
    const c = parseConfig({})!;
    expect(c).toEqual({
      base: {},
      profiles: {},
      local: {},
      secrets: {},
      envLocked: false,
      activeProfile: "default",
      revision: null,
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

  it("normalizes a malformed secret flag", () => {
    const c = parseConfig(rawConfig({ secrets: { TG: { set: "yes", source: 9 } } }))!;
    expect(c.secrets.TG).toEqual({ set: false, source: null });
  });

  it("returns null for non-objects", () => {
    expect(parseConfig(null)).toBeNull();
    expect(parseConfig("nope")).toBeNull();
  });
});

describe("parseValidate", () => {
  it("extracts the errors array of strings", () => {
    expect(parseValidate({ errors: ["bad grid", "unknown server 'x'"] })).toEqual([
      "bad grid",
      "unknown server 'x'",
    ]);
  });
  it("returns [] for junk or missing errors", () => {
    expect(parseValidate({})).toEqual([]);
    expect(parseValidate(null)).toEqual([]);
    expect(parseValidate({ errors: [1, "ok", null] })).toEqual(["ok"]);
  });
});

describe("commandTransport", () => {
  it("maps each method to its invoke command with the right args", async () => {
    const calls: { cmd: string; args?: Record<string, unknown> }[] = [];
    const invoke = async (cmd: string, args?: Record<string, unknown>) => {
      calls.push({ cmd, args });
      if (cmd === "config_secret_set" || cmd === "config_secret_clear") return 204;
      return {};
    };
    const t = commandTransport(invoke);
    const body = { base: {}, profiles: {}, local: {} };
    await t.read();
    await t.validate(body);
    await t.write(body);
    await t.setActive("mobile");
    expect(await t.setSecret("TOK", "v")).toBe(204);
    expect(await t.clearSecret("TOK")).toBe(204);
    expect(calls).toEqual([
      { cmd: "config_read", args: undefined },
      { cmd: "config_validate", args: { body } },
      { cmd: "config_write", args: { body } },
      { cmd: "config_set_active", args: { name: "mobile" } },
      { cmd: "config_secret_set", args: { tokenEnv: "TOK", value: "v" } },
      { cmd: "config_secret_clear", args: { tokenEnv: "TOK" } },
    ]);
  });
});

describe("toWriteBody", () => {
  it("drops secrets and deep-copies base/profiles/local", () => {
    const payload = parseConfig(rawConfig())!;
    const body = toWriteBody(payload);
    expect(body).toEqual({
      base: { servers: [{ id: "local", url: "ws://x", token_env: "TOK" }], deck: { grid: "5x3" } },
      profiles: { mobile: { view: { management: "bottom_row" } } },
      local: { active_profile: "mobile" },
    });
    expect("secrets" in body).toBe(false);
    // deep copy: mutating the body must not touch the source payload
    (body.base.deck as Record<string, unknown>).grid = "4x3";
    expect(payload.base.deck).toEqual({ grid: "5x3" });
  });
});

describe("inheritedValue", () => {
  it("returns the base section value, or undefined when absent", () => {
    const base = { view: { management: "launcher_menu" } };
    expect(inheritedValue(base, "view", "management")).toBe("launcher_menu");
    expect(inheritedValue(base, "view", "agent_slots")).toBeUndefined();
    expect(inheritedValue(base, "deck", "grid")).toBeUndefined();
  });
});

describe("setOverride / clearOverride", () => {
  it("setOverride writes a new profiles map without touching the input", () => {
    const profiles = { mobile: {} as Record<string, unknown> };
    const next = setOverride(profiles, "mobile", "view", "management", "bottom_row");
    expect(next.mobile.view).toEqual({ management: "bottom_row" });
    expect(profiles.mobile).toEqual({}); // input untouched
  });

  it("clearOverride removes the key and prunes empty section/profile", () => {
    const profiles = { mobile: { view: { management: "bottom_row" } } };
    const next = clearOverride(profiles, "mobile", "view", "management");
    expect(next.mobile.view).toBeUndefined(); // empty section pruned
    expect(profiles.mobile.view).toEqual({ management: "bottom_row" }); // input untouched
  });

  it("clearOverride on an absent key is a harmless no-op copy", () => {
    const profiles = { mobile: { view: { management: "bottom_row" } } };
    const next = clearOverride(profiles, "mobile", "deck", "grid");
    expect(next.mobile.view).toEqual({ management: "bottom_row" });
  });
});

describe("secretFlag", () => {
  it("returns the flag or a not-set default", () => {
    const payload = parseConfig(rawConfig())!;
    expect(secretFlag(payload, "TOK")).toEqual({ set: true, source: "env" });
    expect(secretFlag(payload, "MISSING")).toEqual({ set: false, source: null });
  });
});

import { serversOf, addServer, removeServer, updateServer } from "./configClient";

describe("server mutations", () => {
  it("serversOf returns the base server list or []", () => {
    expect(serversOf(parseConfig(rawConfig())!)).toEqual([
      { id: "local", url: "ws://x", token_env: "TOK" },
    ]);
    expect(serversOf(parseConfig({})!)).toEqual([]);
  });

  it("addServer appends a blank server without touching the input", () => {
    const p = parseConfig(rawConfig())!;
    const next = addServer(p);
    expect(serversOf(next)).toHaveLength(2);
    expect(serversOf(next)[1]).toEqual({ id: "", url: "", token_env: "" });
    expect(serversOf(p)).toHaveLength(1); // input untouched
  });

  it("updateServer sets one field on a copy", () => {
    const p = parseConfig(rawConfig())!;
    const next = updateServer(p, 0, "url", "ws://new");
    expect(serversOf(next)[0].url).toBe("ws://new");
    expect(serversOf(p)[0].url).toBe("ws://x"); // input untouched
  });

  it("removeServer drops the indexed server", () => {
    const p = addServer(parseConfig(rawConfig())!);
    const next = removeServer(p, 0);
    expect(serversOf(next)).toHaveLength(1);
    expect(serversOf(next)[0]).toEqual({ id: "", url: "", token_env: "" });
  });
});

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

  it("deleteProfile drops a now-dangling base (top-level) active_profile", () => {
    // řez-4a read() carries a legacy top-level active_profile into base for round-trip;
    // deleting that profile must clear it too, else Apply writes an unknown active profile.
    const p = parseConfig({ profiles: { mobile: {} }, base: { active_profile: "mobile", deck: { grid: "5x3" } } })!;
    const next = deleteProfile(p, "mobile");
    expect("active_profile" in next.base).toBe(false); // dangling base selector cleared
    expect((next.base.deck as Record<string, unknown>).grid).toBe("5x3"); // sibling kept
  });
});

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

  it("inheritedFor falls back to base when an unknown target appears after a valid parent", () => {
    const unk = parseConfig({
      base: { view: { management: "launcher_menu" } },
      profiles: {
        parent: { extends: "ghost", view: { management: "bottom_row" } },
        child: { extends: "parent", view: {} },
      },
    })!;
    // child → parent → "ghost" (missing); unknown grandparent breaks the chain → base value
    expect(inheritedFor(unk, "child", "view", "management")).toBe("launcher_menu");
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

describe("inheritedSection (raw chain merge + presence)", () => {
  const base = (sp: unknown) => ({
    base: (sp === undefined ? {} : { start_profiles: sp }) as Record<string, unknown>,
    profiles: {} as Record<string, Record<string, unknown>>,
    local: {}, secrets: {}, activeProfile: "default", envLocked: false,
  });
  it("returns base section (present) when profile extends default", () => {
    const p: any = base({ codex: ["codex"] }); p.profiles = { dev: {} };
    expect(inheritedSection(p, "dev", "start_profiles")).toEqual({ present: true, map: { codex: ["codex"] } });
  });
  it("merges parent overlays per-key, excluding the profile's own overlay", () => {
    const p: any = base({ codex: ["codex"] });
    p.profiles = {
      mid: { extends: "default", start_profiles: { claude: ["claude"] } },
      dev: { extends: "mid", start_profiles: { codex: ["codex", "--yolo"] } },
    };
    expect(inheritedSection(p, "dev", "start_profiles").map).toEqual({ codex: ["codex"], claude: ["claude"] });
  });
  it("falls back to base on a cycle or unknown extends target", () => {
    const p: any = base({ codex: ["codex"] });
    p.profiles = { dev: { extends: "ghost", start_profiles: { x: ["x"] } } };
    expect(inheritedSection(p, "dev", "start_profiles").map).toEqual({ codex: ["codex"] });
  });
  it("present=false when absent everywhere, present=true for explicit {}", () => {
    const absent: any = base(undefined); absent.profiles = { dev: {} };
    expect(inheritedSection(absent, "dev", "start_profiles")).toEqual({ present: false, map: {} });
    const empty: any = base({}); empty.profiles = { dev: {} };
    expect(inheritedSection(empty, "dev", "start_profiles")).toEqual({ present: true, map: {} });
  });
});

describe("default-aware inherited resolvers", () => {
  const mk = (baseObj: Record<string, unknown>): any => ({ base: baseObj, profiles: { dev: {} }, local: {}, secrets: {}, activeProfile: "default", envLocked: false });
  it("inheritedStartProfiles: absent → DEFAULT_START_PROFILES, {} → none, custom → custom", () => {
    expect(inheritedStartProfiles(mk({}), "dev")).toEqual(DEFAULT_START_PROFILES);
    expect(inheritedStartProfiles(mk({ start_profiles: {} }), "dev")).toEqual({});
    expect(inheritedStartProfiles(mk({ start_profiles: { x: ["x"] } }), "dev")).toEqual({ x: ["x"] });
  });
  it("inheritedMacros: absent → DEFAULT_MACROS, [] → none, custom → custom", () => {
    expect(inheritedMacros(mk({}), "dev")).toEqual(DEFAULT_MACROS);
    expect(inheritedMacros(mk({ macros: [] }), "dev")).toEqual([]);
    expect(inheritedMacros(mk({ macros: [{ label: "a", text: "b" }] }), "dev")).toEqual([{ label: "a", text: "b" }]);
  });
  it("inheritedAnswerProfiles: built-ins always present, config overrides per-name", () => {
    const r = inheritedAnswerProfiles(mk({}), "dev");
    expect(Object.keys(r).sort()).toEqual(["claude", "codex", "default"]);
    const r2 = inheritedAnswerProfiles(mk({ answer_profiles: { claude: { approve: ["x"], deny: [], stop: [] }, mine: { approve: ["m"], deny: [], stop: [] } } }), "dev");
    expect((r2.claude as any).approve).toEqual(["x"]); // config replaces the built-in claude entry
    expect(Object.keys(r2).sort()).toEqual(["claude", "codex", "default", "mine"]); // mine added, built-ins kept
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

import { effectiveProfileServers } from "./configClient";

describe("effectiveProfileServers (seed = backend inherited selection)", () => {
  const srv = (id: string) => ({ id, url: "u", token_env: "" });
  const mk = (
    profiles: Record<string, Record<string, unknown>>,
    deck?: Record<string, unknown>,
  ): any => ({
    base: deck ? { servers: [srv("a"), srv("b")], deck } : { servers: [srv("a"), srv("b")] },
    profiles, local: {}, secrets: {}, activeProfile: "default", envLocked: false,
  });
  it("falls back to all base server ids when nothing restricts", () => {
    expect(effectiveProfileServers(mk({ dev: {} }), "dev").sort()).toEqual(["a", "b"]);
  });
  it("uses merged deck.overview_order when present and no parent servers override", () => {
    expect(effectiveProfileServers(mk({ dev: {} }, { overview_order: ["b"] }), "dev")).toEqual(["b"]);
  });
  it("nearest parent profile's servers override wins over overview_order", () => {
    const p = mk({ mid: { servers: ["a"] }, dev: { extends: "mid" } }, { overview_order: ["b"] });
    expect(effectiveProfileServers(p, "dev")).toEqual(["a"]);
  });
  it("includes the profile's own deck overlay overview_order", () => {
    expect(effectiveProfileServers(mk({ dev: { deck: { overview_order: ["a"] } } }), "dev")).toEqual(["a"]);
  });
});

function emptyPayload() {
  return parseConfig({ base: {}, profiles: {}, local: {}, secrets: {} })!;
}

describe("toggle-deck hotkey helpers", () => {
  it("returns the default when the key is absent", () => {
    expect(toggleDeckHotkey(emptyPayload())).toBe(DEFAULT_TOGGLE_DECK_HOTKEY);
  });

  it("returns an explicit empty string (= disabled) verbatim", () => {
    const p = parseConfig({ base: { hotkeys: { toggle_deck: "" } } })!;
    expect(toggleDeckHotkey(p)).toBe("");
  });

  it("returns a configured accelerator verbatim", () => {
    const p = parseConfig({ base: { hotkeys: { toggle_deck: "Alt+Space" } } })!;
    expect(toggleDeckHotkey(p)).toBe("Alt+Space");
  });

  it("writes the accelerator into base.hotkeys.toggle_deck", () => {
    const p = setToggleDeckHotkey(emptyPayload(), "Ctrl+Shift+K");
    expect(toggleDeckHotkey(p)).toBe("Ctrl+Shift+K");
    expect((p.base.hotkeys as Record<string, unknown>).toggle_deck).toBe("Ctrl+Shift+K");
  });
});

describe("window mode", () => {
  it("defaults to normal when absent", () => {
    expect(windowMode(emptyPayload())).toBe(DEFAULT_WINDOW_MODE);
    expect(DEFAULT_WINDOW_MODE).toBe("normal");
  });

  it("returns a stored valid mode", () => {
    const p = setWindowMode(emptyPayload(), "always_on_top");
    expect(windowMode(p)).toBe("always_on_top");
  });

  it("falls back to default for an unknown stored value", () => {
    const p = setAt(emptyPayload(), "base", "desktop", "window_mode", "bogus");
    expect(windowMode(p)).toBe("normal");
  });

  it("exposes exactly the three modes", () => {
    expect(WINDOW_MODES).toEqual(["normal", "floating", "always_on_top"]);
  });
});

describe("effectiveLanguage", () => {
  const base = (lang?: string) => ({
    base: { view: lang ? { language: lang } : {} },
    profiles: {
      night: { view: { language: "cs" } },
      child: { extends: "night" },
      plain: {},
    },
    local: {},
    secrets: {},
  });

  it("reads base view.language when no profile is active (en default)", () => {
    expect(effectiveLanguage(parseConfig(base())!)).toBe("en");
    expect(effectiveLanguage(parseConfig(base("cs"))!)).toBe("cs");
  });

  it("prefers the active profile's own override", () => {
    const p = parseConfig({ ...base("en"), active_profile: "night" })!;
    expect(effectiveLanguage(p)).toBe("cs");
  });

  it("follows the extends chain for an inheriting profile", () => {
    const p = parseConfig({ ...base("en"), active_profile: "child" })!;
    expect(effectiveLanguage(p)).toBe("cs");
  });

  it("falls back to base for a profile without an override", () => {
    const p = parseConfig({ ...base("cs"), active_profile: "plain" })!;
    expect(effectiveLanguage(p)).toBe("cs");
  });
});

describe("errorCountLabel", () => {
  it("defaults to English", () => {
    expect(errorCountLabel(1)).toBe("1 error");
    expect(errorCountLabel(5)).toBe("5 errors");
  });

  it("pluralizes Czech counts", () => {
    expect(errorCountLabel(1, "cs")).toBe("1 chyba");
    expect(errorCountLabel(2, "cs")).toBe("2 chyby");
    expect(errorCountLabel(4, "cs")).toBe("4 chyby");
    expect(errorCountLabel(5, "cs")).toBe("5 chyb");
    expect(errorCountLabel(11, "cs")).toBe("11 chyb");
  });
});

describe("revision staleness guard", () => {
  it("round-trips the revision through parse and write body", () => {
    const payload = parseConfig({
      base: {}, profiles: {}, local: {}, secrets: {},
      env_locked: false, active_profile: "default", revision: "abc123",
    });
    expect(payload?.revision).toBe("abc123");
    expect(toWriteBody(payload!).revision).toBe("abc123");
  });

  it("omits an unknown revision (older sidecars)", () => {
    const payload = parseConfig({
      base: {}, profiles: {}, local: {}, secrets: {},
      env_locked: false, active_profile: "default",
    });
    expect(payload?.revision).toBeNull();
    expect("revision" in toWriteBody(payload!)).toBe(false);
  });

  it("classifies the stale error", () => {
    expect(isStaleRevisionError("stale_revision: config changed on disk")).toBe(true);
    expect(isStaleRevisionError("grid must be WxH")).toBe(false);
  });
});
