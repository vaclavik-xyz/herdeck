import { describe, expect, it } from "vitest";
import { appSurface } from "./appSurface";

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
