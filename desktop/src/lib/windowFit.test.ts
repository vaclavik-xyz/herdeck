import { describe, it, expect } from "vitest";
import { fitDecision } from "./windowFit";

describe("fitDecision", () => {
  it("applies on first measure (no previous request) and rounds to integer px", () => {
    expect(fitDecision(320.4, null, 360)).toEqual({ apply: true, width: 360, height: 320 });
    expect(fitDecision(320.6, null, 360)).toEqual({ apply: true, width: 360, height: 321 });
  });

  it("skips when within tolerance of the last requested height (anti-feedback)", () => {
    expect(fitDecision(320.4, 320, 360)).toEqual({ apply: false, width: 360, height: 320 });
    expect(fitDecision(319.7, 320, 360)).toEqual({ apply: false, width: 360, height: 320 });
  });

  it("applies when the change exceeds tolerance", () => {
    expect(fitDecision(340, 320, 360)).toEqual({ apply: true, width: 360, height: 340 });
  });

  // The deck window is created hidden; a WebView that has never laid out on
  // screen can measure 0, and setSize(width, 0) is a window that paints nothing
  // when the user finally shows it. Nothing downstream floors the height.
  it("never asks for a zero-height window", () => {
    expect(fitDecision(0, null, 360)).toEqual({ apply: false, width: 360, height: 0 });
    expect(fitDecision(0, 320, 360)).toEqual({ apply: false, width: 360, height: 320 });
    expect(fitDecision(0.4, null, 360)).toEqual({ apply: false, width: 360, height: 0 });
    expect(fitDecision(-12, 320, 360)).toEqual({ apply: false, width: 360, height: 320 });
    expect(fitDecision(Number.NaN, 320, 360)).toEqual({ apply: false, width: 360, height: 320 });
  });

  it("still applies the smallest real measurement", () => {
    expect(fitDecision(0.6, null, 360)).toEqual({ apply: true, width: 360, height: 1 });
  });
});
