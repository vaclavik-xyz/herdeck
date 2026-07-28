import { describe, expect, it } from "vitest";
import { parseConfig, type ConfigPayload } from "./configClient";
import { connectionInventory } from "./connectionStatus";
import { initialView, type DeckViewModel } from "./deckClient";

function payload(): ConfigPayload {
  const parsed = parseConfig({
    base: {
      servers: [
        { id: "macbench", url: "ws://macbench:8788", token_env: "MACBENCH_TOKEN" },
        { id: "lab", url: "ws://lab:8788", token_env: "LAB_TOKEN" },
      ],
      deck: { overview_order: ["macbench"] },
    },
    local: {},
    local_sessions: [
      { name: "personal", server_id: "local:personal", socket_path: "/tmp/personal.sock", available: true, selected: true },
      { name: "sleeping", server_id: "local:sleeping", socket_path: "/tmp/sleeping.sock", available: false, selected: true },
      { name: "unused", server_id: "local:unused", socket_path: "/tmp/unused.sock", available: true, selected: false },
    ],
  });
  if (!parsed) throw new Error("invalid fixture");
  return parsed;
}

function view(overrides: Partial<DeckViewModel> = {}): DeckViewModel {
  return { ...initialView(), ...overrides };
}

describe("connectionInventory", () => {
  it("keeps socket availability separate from a live local connection", () => {
    const result = connectionInventory(payload(), view());

    expect(result.local.map(({ name, health }) => ({ name, health }))).toEqual([
      { name: "personal", health: "disconnected" },
      { name: "sleeping", health: "unavailable" },
      { name: "unused", health: "inactive" },
    ]);
  });

  it("uses the runtime session map instead of the configured server id", () => {
    const result = connectionInventory(payload(), view({
      localConnections: { personal: "local:personal-2" },
      connections: { "local:personal": false, "local:personal-2": true },
    }));

    expect(result.local[0]).toMatchObject({
      runtimeId: "local:personal-2",
      health: "connected",
    });
  });

  it("marks only servers selected by the active profile as live candidates", () => {
    const result = connectionInventory(payload(), view({
      connections: { macbench: true, lab: true },
    }));

    expect(result.remote.map(({ id, active, health }) => ({ id, active, health }))).toEqual([
      { id: "macbench", active: true, health: "connected" },
      { id: "lab", active: false, health: "inactive" },
    ]);
  });
});
