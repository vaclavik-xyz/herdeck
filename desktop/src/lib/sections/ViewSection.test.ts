import { describe, expect, it } from "vitest";
import { mount, unmount } from "svelte";

import { parseConfig } from "../configClient";
import { setLang } from "../i18n.svelte";
import ViewSection from "./ViewSection.svelte";

describe("ViewSection", () => {
  it("derives tile-line defaults from effective tile_fields", () => {
    setLang("en");
    const payload = parseConfig({ base: { view: { tile_fields: ["status"] } } })!;
    const target = document.createElement("div");
    const instance = mount(ViewSection, {
      target,
      props: { payload, onChange: () => {}, onError: () => {} },
    });
    try {
      const fields = Array.from(target.querySelectorAll(".tristate"));
      for (const name of ["tile_primary", "tile_secondary"]) {
        const field = fields.find(
          (item) => item.querySelector(".label")?.textContent?.trim() === name,
        );
        expect(field?.querySelector(".hint")?.textContent).not.toMatch(/repo|branch/);
      }
    } finally {
      unmount(instance);
    }
  });

  it("uses the profile's own tile_fields override for line fallbacks", () => {
    setLang("en");
    const payload = parseConfig({
      profiles: { night: { view: { tile_fields: ["status"] } } },
    })!;
    const target = document.createElement("div");
    const instance = mount(ViewSection, {
      target,
      props: { payload, editProfile: "night", onChange: () => {}, onError: () => {} },
    });
    try {
      const fields = Array.from(target.querySelectorAll(".tristate"));
      for (const name of ["tile_primary", "tile_secondary"]) {
        const field = fields.find(
          (item) => item.querySelector(".label")?.textContent?.trim() === name,
        );
        expect(field?.querySelector(".hint")?.textContent).not.toMatch(/repo|branch/);
      }
    } finally {
      unmount(instance);
    }
  });

  it("shows the Elgato plugin's fixed repo and branch fallbacks", () => {
    setLang("en");
    const payload = parseConfig({
      base: { view: { tile_fields: ["status"] } },
      runtime_deck: "elgato-plugin",
    })!;
    const target = document.createElement("div");
    const instance = mount(ViewSection, {
      target,
      props: { payload, onChange: () => {}, onError: () => {} },
    });
    try {
      const fields = Array.from(target.querySelectorAll(".tristate"));
      const primary = fields.find(
        (item) => item.querySelector(".label")?.textContent?.trim() === "tile_primary",
      );
      const secondary = fields.find(
        (item) => item.querySelector(".label")?.textContent?.trim() === "tile_secondary",
      );
      expect(primary?.querySelector(".hint")?.textContent).toContain("repo");
      expect(secondary?.querySelector(".hint")?.textContent).toContain("branch");
    } finally {
      unmount(instance);
    }
  });
});
