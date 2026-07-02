import { describe, it, expect } from "vitest";
import { DEFAULT_STATUS_COLORS, PALETTE_NAMES, swatchColor } from "./statusColors";

// This mirror must stay in sync with the backend DEFAULT_STATUS_COLORS
// (src/herdeck/config.py). It is the fallback the config editor shows/writes
// when a profile has no inherited theme.colors.<status>, so a stale value here
// silently persists the wrong colour on override toggles.
describe("DEFAULT_STATUS_COLORS (backend mirror)", () => {
  it("maps done to its own visible colour, not dim", () => {
    // done = finished-but-unseen; dim was indistinguishable from empty tiles.
    expect(DEFAULT_STATUS_COLORS.done).toBe("cyan");
  });

  it("mirrors the full backend default set", () => {
    expect(DEFAULT_STATUS_COLORS).toEqual({
      working: "green",
      idle: "blue",
      blocked: "amber",
      done: "cyan",
      unknown: "grey",
      offline: "red",
    });
  });
});

describe("palette", () => {
  it("resolves named colours and hex accents to css", () => {
    expect(swatchColor("amber")).toBe("rgb(230,170,20)");
    expect(swatchColor("#12ab34")).toBe("#12ab34");
    expect(swatchColor("ambre")).toBe("transparent"); // the typo is visibly wrong
  });

  it("covers every default status colour", () => {
    for (const name of Object.values(DEFAULT_STATUS_COLORS)) {
      expect(PALETTE_NAMES).toContain(name);
    }
  });
});
