import { describe, it, expect } from "vitest";
import {
  parseConfig,
  parseValidate,
  commandTransport,
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
