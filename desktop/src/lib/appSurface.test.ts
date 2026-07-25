import { describe, expect, it } from "vitest";
import { appSurface, desktopSetupVisible } from "./appSurface";

describe("appSurface", () => {
  it("uses the desktop control room for the normal window", () => {
    expect(appSurface("normal")).toBe("desktop");
    expect(appSurface(undefined)).toBe("desktop");
  });

  it("keeps the deck-only surface for compact overlay modes", () => {
    expect(appSurface("floating")).toBe("compact");
    expect(appSurface("always_on_top")).toBe("compact");
  });
});

describe("desktopSetupVisible", () => {
  it("layers setup over the mounted desktop only when needed", () => {
    expect(desktopSetupVisible("desktop", "welcome", false)).toBe(true);
    expect(desktopSetupVisible("desktop", "reconnect", false)).toBe(true);
    expect(desktopSetupVisible("desktop", "deck", false)).toBe(false);
    expect(desktopSetupVisible("desktop", "welcome", true)).toBe(false);
    expect(desktopSetupVisible("compact", "welcome", false)).toBe(false);
  });
});
