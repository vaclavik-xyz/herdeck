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
});
