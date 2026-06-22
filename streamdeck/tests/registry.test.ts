import { describe, it, expect, vi } from "vitest";
import { KeyRegistry } from "../src/registry.js";

describe("KeyRegistry", () => {
  it("tracks slots + action keys and yields full snapshots", () => {
    const r = new KeyRegistry();
    r.addSlot("s0", { col: 0, row: 0 });
    r.addSlot("s1", { col: 1, row: 0 });
    r.addActionKey("a", "approve", { col: 0, row: 2 });
    expect(r.slotsSnapshot()).toEqual([
      { instanceId: "s0", coord: { col: 0, row: 0 } },
      { instanceId: "s1", coord: { col: 1, row: 0 } },
    ]);
    expect(r.actionKeysSnapshot()).toEqual([{ instanceId: "a", type: "approve", coord: { col: 0, row: 2 } }]);
  });

  it("removal drops the key from the next snapshot (no per-key bye)", () => {
    const r = new KeyRegistry();
    r.addSlot("s0", { col: 0, row: 0 });
    r.addSlot("s1", { col: 1, row: 0 });
    r.removeSlot("s0");
    expect(r.slotsSnapshot()).toEqual([{ instanceId: "s1", coord: { col: 1, row: 0 } }]);
  });

  it("fires onChange after each mutation", () => {
    const r = new KeyRegistry();
    const cb = vi.fn();
    r.onChange(cb);
    r.addSlot("s0", { col: 0, row: 0 });
    r.addActionKey("a", "stop", { col: 2, row: 2 });
    r.removeSlot("s0");
    expect(cb).toHaveBeenCalledTimes(3);
  });
});
