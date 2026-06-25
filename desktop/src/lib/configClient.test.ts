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
    expect(c).toEqual({ base: {}, profiles: {}, local: {}, secrets: {} });
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
