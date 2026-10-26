import { describe, expect, it } from "vitest";
import { appSurface, desktopSetupVisible, windowRole, floatingContent } from "./appSurface";

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
// desktop/src-tauri/src/window_roles.rs, called from `run()` in lib.rs). Rust's
// own test (`the_role_script_sets_the_attribute_the_frontend_reads`, in
// plan_tests.rs) pins the attribute-name/format half of that contract from its
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

// Rust stamps `data-floating` = "dock" | "deck" before first paint
// (deck_prefs::floating_script; pinned there by
// the_floating_script_stamps_the_attribute_and_prefers_a_reloaded_choice).
// This pins the frontend's half: the dataset key and the dock default.
describe("floatingContent / the cross-language floating contract", () => {
  it("reads dataset.floating, defaulting to the dock", () => {
    const doc = document.implementation.createHTMLDocument("");
    expect(floatingContent(doc)).toBe("dock");
    doc.documentElement.dataset.floating = "deck";
    expect(floatingContent(doc)).toBe("deck");
    doc.documentElement.dataset.floating = "dock";
    expect(floatingContent(doc)).toBe("dock");
    doc.documentElement.dataset.floating = "sideways";
    expect(floatingContent(doc)).toBe("dock");
    expect(floatingContent(undefined)).toBe("dock");
  });
});
