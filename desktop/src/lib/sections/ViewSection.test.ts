import { describe, expect, it } from "vitest";
import { flushSync, mount, unmount } from "svelte";

import { parseConfig } from "../configClient";
import { setLang } from "../i18n.svelte";
import ViewSection from "./ViewSection.svelte";

describe("ViewSection", () => {
  it.each([undefined, "night"])("toggles backend labels while preserving other fields (%s)", (editProfile) => {
    setLang("en");
    const payload = parseConfig({ base: { view: { tile_fields: ["repo", "status", "server", "profile"] } }, profiles: { night: { view: {} } } })!;
    const target = document.createElement("div");
    let changes = 0;
    const instance = mount(ViewSection, { target, props: { payload, editProfile, onChange: () => changes++, onError: () => {} } });
    try {
      const toggle = Array.from(target.querySelectorAll("label.field")).find(item => item.textContent?.includes("Show T3 / HERDR labels"))?.querySelector<HTMLInputElement>("input")!;
      expect(toggle.checked).toBe(true);
      const tileFields = () => Array.from(target.querySelectorAll(".tristate")).find(item => item.querySelector<HTMLElement>("[data-config-key]")?.dataset.configKey === "tile_fields")!;
      const values = () => Array.from(tileFields().querySelectorAll<HTMLInputElement>('input')).map(input => input.value);
      flushSync(() => { toggle.checked = false; toggle.dispatchEvent(new Event("change", { bubbles: true })); });
      expect(values()).toEqual(["repo", "status", "profile"]);
      expect(toggle.checked).toBe(false);
      flushSync(() => { toggle.checked = true; toggle.dispatchEvent(new Event("change", { bubbles: true })); });
      expect(values()).toEqual(["repo", "status", "profile", "server"]);
      expect(changes).toBe(2);
      // A profile edit must never mutate its inherited base configuration.
      if (editProfile) expect(payload.base.view.tile_fields).toEqual(["repo", "status", "server", "profile"]);
    } finally { unmount(instance); }
  });

  it("offers native Herdr ordering without changing the default", () => {
    setLang("en");
    const payload = parseConfig({ base: { view: {} } })!;
    const target = document.createElement("div");
    const instance = mount(ViewSection, {
      target,
      props: { payload, onChange: () => {}, onError: () => {} },
    });
    try {
      const field = Array.from(target.querySelectorAll("label.field")).find(
        (item) => item.querySelector<HTMLElement>("[data-config-key]")?.dataset.configKey === "agent_order",
      );
      const select = field?.querySelector<HTMLSelectElement>("select");
      expect(select?.value).toBe("status");
      expect(Array.from(select?.options ?? []).map((option) => option.value)).toEqual(["status", "herdr"]);
    } finally {
      unmount(instance);
    }
  });

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
          (item) => item.querySelector<HTMLElement>("[data-config-key]")?.dataset.configKey === name,
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
          (item) => item.querySelector<HTMLElement>("[data-config-key]")?.dataset.configKey === name,
        );
        expect(field?.querySelector(".hint")?.textContent).not.toMatch(/repo|branch/);
      }
    } finally {
      unmount(instance);
    }
  });

  it("shows the Elgato plugin's fixed repo and tab-plus-branch fallbacks", () => {
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
        (item) => item.querySelector<HTMLElement>("[data-config-key]")?.dataset.configKey === "tile_primary",
      );
      const secondary = fields.find(
        (item) => item.querySelector<HTMLElement>("[data-config-key]")?.dataset.configKey === "tile_secondary",
      );
      expect(primary?.querySelector(".hint")?.textContent).toContain("repo");
      expect(secondary?.querySelector(".hint")?.textContent).toContain("tab");
      expect(secondary?.querySelector(".hint")?.textContent).toContain("branch");
    } finally {
      unmount(instance);
    }
  });

  it("shows tab before branch in the default D200 secondary line", () => {
    setLang("en");
    const payload = parseConfig({ base: { view: {} } })!;
    const target = document.createElement("div");
    const instance = mount(ViewSection, {
      target,
      props: { payload, onChange: () => {}, onError: () => {} },
    });
    try {
      const secondary = Array.from(target.querySelectorAll(".tristate")).find(
        (item) => item.querySelector<HTMLElement>("[data-config-key]")?.dataset.configKey === "tile_secondary",
      );
      expect(secondary?.querySelector(".hint")?.textContent).toContain("tab · branch");
    } finally {
      unmount(instance);
    }
  });
});
