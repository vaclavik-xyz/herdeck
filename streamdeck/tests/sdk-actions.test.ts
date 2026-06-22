import { describe, it, expect, vi } from "vitest";
import { makeSlotAction, makeActionKey } from "../src/actions/sdk-actions.js";
import { KeyRegistry } from "../src/registry.js";

function fakeAdapter() {
  return { registerSurface: vi.fn(), unregisterSurface: vi.fn(), handleKeyDown: vi.fn(), handleKeyUp: vi.fn() };
}

function mockEv(id: string, coordinates: { column: number; row: number } | undefined) {
  return { action: { id, coordinates, setImage: vi.fn(), setTitle: vi.fn() } } as any;
}

describe("sdk-actions onWillAppear coordinate guard", () => {
  it("slot: registers when coordinates are present", () => {
    const reg = new KeyRegistry();
    const adapter = fakeAdapter();
    const slot = makeSlotAction(reg, adapter as any) as any;
    slot.onWillAppear(mockEv("s0", { column: 1, row: 0 }));
    expect(reg.slotsSnapshot()).toEqual([{ instanceId: "s0", coord: { col: 1, row: 0 } }]);
    expect(adapter.registerSurface).toHaveBeenCalledWith("s0", expect.anything());
  });

  it("slot: skips registration (no TypeError) when coordinates are undefined (Multi-Action)", () => {
    const reg = new KeyRegistry();
    const adapter = fakeAdapter();
    const slot = makeSlotAction(reg, adapter as any) as any;
    expect(() => slot.onWillAppear(mockEv("s0", undefined))).not.toThrow();
    expect(reg.slotsSnapshot()).toEqual([]); // not registered, but the handler survived
    expect(adapter.registerSurface).not.toHaveBeenCalled();
  });

  it("action key: skips registration when coordinates are undefined (Multi-Action)", () => {
    const reg = new KeyRegistry();
    const adapter = fakeAdapter();
    const stop = makeActionKey(reg, adapter as any, "xyz.vaclavik.herdeck.stop", "stop") as any;
    expect(() => stop.onWillAppear(mockEv("t", undefined))).not.toThrow();
    expect(reg.actionKeysSnapshot()).toEqual([]);
    expect(adapter.registerSurface).not.toHaveBeenCalled();
  });
});
