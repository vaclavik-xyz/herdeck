import { describe, expect, it } from "vitest";
import { appSurface, desktopSetupVisible, windowRole } from "./appSurface";

describe("appSurface", () => {
  it("routes by window ROLE, not by a window mode", () => {
    expect(appSurface("app")).toBe("desktop");
    expect(appSurface("deck")).toBe("compact");
  });

  // A plain browser has no injected role; the settings surface is the one worth
  // designing against, and it is what the config window shows.
  it("defaults to the desktop surface", () => {
    expect(appSurface(undefined)).toBe("desktop");
    expect(appSurface(null)).toBe("desktop");
    expect(appSurface("sideways")).toBe("desktop");
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

// Rust stamps `document.documentElement.dataset.windowRole` with exactly "app"
// or "deck" before first paint (`window_role_script` + its two call sites in
// desktop/src-tauri/src/lib.rs, ~line 1227 and ~lines 1853/1873). Rust's own
// test (`the_role_script_sets_the_attribute_the_frontend_reads`, same file,
// ~line 740) pins the attribute-name/format half of that contract from its
// side. This test pins the frontend's half: the exact dataset key AND both
// literal role values, read through the real `windowRole()` helper that
// App.svelte calls — so a typo in either language (a renamed key, a
// misspelled role) fails a test here instead of silently rendering the wrong
// surface.
describe("windowRole / the cross-language role contract", () => {
  it("reads the exact dataset key Rust's initialization_script sets", () => {
    const doc = document.implementation.createHTMLDocument("");
    doc.documentElement.dataset.windowRole = "deck";
    expect(windowRole(doc)).toBe("deck");
    expect(appSurface(windowRole(doc))).toBe("compact");

    doc.documentElement.dataset.windowRole = "app";
    expect(windowRole(doc)).toBe("app");
    expect(appSurface(windowRole(doc))).toBe("desktop");
  });

  it("has nothing to read from a document without the attribute, or none at all", () => {
    const doc = document.implementation.createHTMLDocument("");
    expect(windowRole(doc)).toBeUndefined();
    expect(windowRole(undefined)).toBeUndefined();
  });
});
