import { describe, it, expect, vi } from "vitest";
import { KeyRegistry } from "../src/registry.js";
import {
  coordToWire,
  onSlotAppear,
  onSlotDisappear,
  onActionAppear,
  onActionDisappear,
} from "../src/actions/core.js";

function fakeAdapter() {
  return { registerSurface: vi.fn(), unregisterSurface: vi.fn(), handleKeyDown: vi.fn(), handleKeyUp: vi.fn() };
}

describe("action <-> core delegation (SDK-free)", () => {
  it("maps SDK coordinates.column -> wire coord.col", () => {
    expect(coordToWire({ column: 3, row: 2 })).toEqual({ col: 3, row: 2 });
  });

  it("slot appear registers a slot + surface; disappear removes both", () => {
    const reg = new KeyRegistry();
    const adapter = fakeAdapter();
    const surface = { setImage() {}, setTitle() {} };
    onSlotAppear(reg, adapter as any, "s0", { col: 0, row: 0 }, surface);
    expect(reg.slotsSnapshot()).toEqual([{ instanceId: "s0", coord: { col: 0, row: 0 } }]);
    expect(adapter.registerSurface).toHaveBeenCalledWith("s0", surface);
    onSlotDisappear(reg, adapter as any, "s0");
    expect(reg.slotsSnapshot()).toEqual([]);
    expect(adapter.unregisterSurface).toHaveBeenCalledWith("s0");
  });

  it("action-key appear registers typed action + surface; disappear removes", () => {
    const reg = new KeyRegistry();
    const adapter = fakeAdapter();
    const surface = { setImage() {}, setTitle() {} };
    onActionAppear(reg, adapter as any, "a", "approve", { col: 0, row: 2 }, surface);
    expect(reg.actionKeysSnapshot()).toEqual([{ instanceId: "a", type: "approve", coord: { col: 0, row: 2 } }]);
    expect(adapter.registerSurface).toHaveBeenCalledWith("a", surface);
    onActionDisappear(reg, adapter as any, "a");
    expect(reg.actionKeysSnapshot()).toEqual([]);
  });
});
