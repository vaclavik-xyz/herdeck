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
  type ConfigPayload,
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
