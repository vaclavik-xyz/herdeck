import { describe, expect, it } from "vitest";
import { floatingScaleCommandFromEvent, shouldBypassDeckContextMenu } from "./deckContextMenu";

describe("deck context menu", () => {
  it("accepts only the three known zoom commands", () => {
    expect(floatingScaleCommandFromEvent("in")).toBe("in");
    expect(floatingScaleCommandFromEvent("out")).toBe("out");
    expect(floatingScaleCommandFromEvent("reset")).toBe("reset");
    expect(floatingScaleCommandFromEvent("zoom")).toBeNull();
    expect(floatingScaleCommandFromEvent(1)).toBeNull();
    expect(floatingScaleCommandFromEvent(null)).toBeNull();
    expect(floatingScaleCommandFromEvent(undefined)).toBeNull();
  });

  it("bypasses the custom menu only when Shift is held", () => {
    expect(shouldBypassDeckContextMenu({ shiftKey: true })).toBe(true);
    expect(shouldBypassDeckContextMenu({ shiftKey: false })).toBe(false);
  });
});
