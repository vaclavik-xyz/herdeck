import { describe, it, expect } from "vitest";
import DesktopSection from "./DesktopSection.svelte";
import ViewSection from "./ViewSection.svelte";
import ThemeSection from "./ThemeSection.svelte";

// Compile-smoke only: importing a .svelte compiles it (catches syntax/compile
// errors) without a render harness.
describe("section compile-smoke", () => {
  it("compiles DesktopSection", () => {
    expect(DesktopSection).toBeTruthy();
  });

  it("compiles ViewSection", () => {
    expect(ViewSection).toBeTruthy();
  });

  it("compiles ThemeSection", () => {
    expect(ThemeSection).toBeTruthy();
  });
});
