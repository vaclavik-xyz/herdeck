import { describe, expect, it } from "vitest";
import { effectiveActiveServerIds, type ConfigPayload } from "./configClient";

const server = (id: string) => ({ id, url: "ws://example:8788", token_env: "" });

function payload(
  activeProfile: string,
  profiles: Record<string, Record<string, unknown>> = {},
): ConfigPayload {
  return {
    base: {
      servers: [server("a"), server("b")],
      deck: { overview_order: ["b", "local"] },
    },
    profiles,
    local: {},
    secrets: {},
    envLocked: false,
    activeProfile,
    runtimeDeck: null,
    localSessions: [],
    revision: null,
  };
}

describe("effectiveActiveServerIds", () => {
  it("uses base overview_order for the default profile", () => {
    expect(effectiveActiveServerIds(payload("default"))).toEqual(["b", "local"]);
  });

  it("uses a named profile's explicit server selection", () => {
    expect(effectiveActiveServerIds(payload("dev", { dev: { servers: ["a"] } }))).toEqual(["a"]);
  });

  it("inherits the effective deck selection when a named profile has no server list", () => {
    expect(effectiveActiveServerIds(payload("dev", { dev: {} }))).toEqual(["b", "local"]);
  });
});
