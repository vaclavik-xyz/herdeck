import { describe, expect, it } from "vitest";
import {
  changeFloatingScale,
  anchoredFloatingPosition,
  floatingScaleCommandForKey,
  floatingFrameSize,
  floatingViewport,
  readFloatingScale,
  writeFloatingScale,
} from "./floatingScale";

describe("floating scale", () => {
  it("restores a valid saved scale and otherwise starts at 100%", () => {
    const storage = new Map<string, string>();
    const reader = { getItem: (key: string) => storage.get(key) ?? null };

    expect(readFloatingScale(reader)).toBe(1);
    storage.set("herdeck.floatingScale", "1.2");
    expect(readFloatingScale(reader)).toBe(1.2);
    storage.set("herdeck.floatingScale", "not-a-number");
    expect(readFloatingScale(reader)).toBe(1);
  });

  it("changes scale in 10% steps and clamps it to 80–140%", () => {
    expect(changeFloatingScale(1, "in")).toBe(1.1);
    expect(changeFloatingScale(1, "out")).toBe(0.9);
    expect(changeFloatingScale(1.4, "in")).toBe(1.4);
    expect(changeFloatingScale(0.8, "out")).toBe(0.8);
    expect(changeFloatingScale(1.3, "reset")).toBe(1);
  });

  it("maps command zoom shortcuts and ignores unmodified typing", () => {
    const key = (value: string, modified = true) => ({
      key: value,
      metaKey: modified,
      ctrlKey: false,
      altKey: false,
    });

    expect(floatingScaleCommandForKey(key("="))).toBe("in");
    expect(floatingScaleCommandForKey(key("+"))).toBe("in");
    expect(floatingScaleCommandForKey(key("-"))).toBe("out");
    expect(floatingScaleCommandForKey(key("0"))).toBe("reset");
    expect(floatingScaleCommandForKey(key("+", false))).toBeNull();
  });

  it("stores only bounded scale values", () => {
    const storage = new Map<string, string>();
    const writer = { setItem: (key: string, value: string) => storage.set(key, value) };
    const reader = { getItem: (key: string) => storage.get(key) ?? null };

    expect(writeFloatingScale(writer, 1.8)).toBe(1.4);
    expect(readFloatingScale(reader)).toBe(1.4);
    storage.set("herdeck.floatingScale", "0.2");
    expect(readFloatingScale(reader)).toBe(0.8);
  });

  it("keeps an edge-docked palette on screen while its size changes", () => {
    const monitor = { x: 0, y: 0, width: 2048, height: 1280 };

    expect(anchoredFloatingPosition(
      { x: 1704, y: 16 },
      { width: 328, height: 291 },
      { width: 394, height: 349 },
      monitor,
    )).toEqual({ x: 1638, y: 16 });
    expect(anchoredFloatingPosition(
      { x: 500, y: 400 },
      { width: 328, height: 291 },
      { width: 394, height: 349 },
      monitor,
    )).toEqual({ x: 500, y: 400 });
    expect(anchoredFloatingPosition(
      { x: 500, y: 400 },
      { width: 328, height: 291 },
      { width: 2200, height: 1400 },
      monitor,
    )).toEqual({ x: 0, y: 0 });
  });

  it("enables scrolling only when scaled content exceeds the work area", () => {
    expect(floatingViewport(291, 0.8, 1000)).toEqual({
      height: 233,
      scrollable: false,
    });
    expect(floatingViewport(1400, 1, 1000)).toEqual({
      height: 1000,
      scrollable: true,
    });
  });

  it("sizes the scroll frame to the transformed card instead of its base box", () => {
    expect(floatingFrameSize(1400, 0.8)).toEqual({
      width: 262,
      height: 1120,
    });
  });
});
