import { describe, expect, it } from "vitest";
import { flushSync, mount, unmount } from "svelte";

import { parseConfig, type ConfigPayload } from "../configClient";
import { reactiveProps } from "../testProps.svelte";
import { setLang } from "../i18n.svelte";
import ViewSection from "./ViewSection.svelte";

describe("ViewSection", () => {
  it.each([undefined, "night"])("switches the prominent heading and preserves unrelated fields (%s)", (editProfile) => {
    setLang("en");
    const payload = parseConfig({ base: { view: { tile_fields: ["repo", "tab", "server"] } }, profiles: { night: { view: {} } } })!;
    const target = document.createElement("div");
    let changes = 0;
    const instance = mount(ViewSection, { target, props: { payload, editProfile, onChange: () => changes++, onError: () => {} } });
    try {
      const select = Array.from(target.querySelectorAll("label.field")).find(item => item.textContent?.includes("Emphasize"))?.querySelector<HTMLSelectElement>("select")!;
      const values = (key: string) => Array.from(Array.from(target.querySelectorAll(".tristate")).find(item => item.querySelector<HTMLElement>("[data-config-key]")?.dataset.configKey === key)!.querySelectorAll<HTMLInputElement>("input")).map(input => input.value);
      expect(select.value).toBe("Project");
      flushSync(() => { select.value = "Thread"; select.dispatchEvent(new Event("change", { bubbles: true })); });
      expect(values("tile_primary")).toEqual(["tab"]);
      expect(values("tile_secondary")).toEqual(["project"]);
      expect(changes).toBe(1);
      flushSync(() => { select.value = "Project"; select.dispatchEvent(new Event("change", { bubbles: true })); });
      expect(values("tile_primary")).toEqual(["project"]);
      expect(values("tile_secondary")).toEqual(["tab", "branch"]);
      expect(payload.base.view.tile_fields).toEqual(["repo", "tab", "server"]);
      if (editProfile) expect(payload.base.view.tile_primary).toBeUndefined();
    } finally { unmount(instance); }
  });

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

  it("shows the Elgato plugin's fixed project and tab-plus-branch fallbacks", () => {
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
      expect(primary?.querySelector(".hint")?.textContent).toContain("project");
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
  it("shows the TOML key chip on prose-labelled fields", () => {
    setLang("en");
    const payload = parseConfig({ base: { view: {} }, profiles: {} })!;
    const target = document.createElement("div");
    const instance = mount(ViewSection, { target, props: { payload, onChange: () => {}, onError: () => {} } });
    try {
      const chip = (text: string) => Array.from(target.querySelectorAll("label.field"))
        .find(item => item.textContent?.includes(text))?.querySelector(".fieldlabel code")?.textContent;
      expect(chip("Emphasize")).toBe("tile_primary · tile_secondary");
      expect(chip("Show T3 / HERDR labels")).toBe("tile_fields");
      expect(chip("Controls layout")).toBe("management");
    } finally { unmount(instance); }
  });
});

describe("ViewSection project icons", () => {
  function mountView(payload: ConfigPayload, editProfile?: string, errors: string[] = []) {
    const props = reactiveProps<Record<string, unknown>>({
      payload, editProfile, onChange: () => {}, onError: (m: string) => errors.push(m),
    });
    const target = document.createElement("div");
    const instance = mount(ViewSection, { target, props: props as never });
    const view = () => ((props.payload as ConfigPayload).base.view ?? {}) as Record<string, unknown>;
    return { props, target, instance, view };
  }
  const iconsField = (target: HTMLElement) => target.querySelector<HTMLElement>(".project-icons")!;
  const type = (input: HTMLInputElement, value: string) =>
    flushSync(() => { input.value = value; input.dispatchEvent(new Event("input", { bubbles: true })); });
  const addRow = (field: HTMLElement) => flushSync(() => field.querySelector<HTMLButtonElement>("button.add")!.click());

  it("offers tile_icon with agent as the default", () => {
    setLang("en");
    const { target, instance, view } = mountView(parseConfig({ base: { view: {} } })!);
    try {
      const select = Array.from(target.querySelectorAll("label.field"))
        .find((i) => i.querySelector<HTMLElement>("[data-config-key]")?.dataset.configKey === "tile_icon")!
        .querySelector("select")!;
      expect(select.value).toBe("agent");
      expect(Array.from(select.options).map((o) => o.value)).toEqual(["agent", "project", "both"]);
      flushSync(() => { select.value = "both"; select.dispatchEvent(new Event("change", { bubbles: true })); });
      expect(view().tile_icon).toBe("both");
    } finally { unmount(instance); }
  });

  it("writes complete rows into view.project_icons and removes them again", () => {
    setLang("en");
    const { target, instance, view } = mountView(parseConfig({ base: { view: {} } })!);
    try {
      const field = iconsField(target);
      expect(field.querySelector("[data-config-key]")?.getAttribute("data-config-key")).toBe("project_icons");
      addRow(field);
      const [repo, path] = Array.from(field.querySelectorAll<HTMLInputElement>(".row input"));
      type(repo, "shop");
      expect(view().project_icons).toBeUndefined(); // no path yet: nothing written
      type(path, "~/icons/shop.png");
      expect(view().project_icons).toEqual({ shop: "~/icons/shop.png" });
      const remove = field.querySelector<HTMLButtonElement>("button.remove")!;
      expect(remove.title).toBe("Remove project icon");
      flushSync(() => remove.click());
      expect(view()).not.toHaveProperty("project_icons");
    } finally { unmount(instance); }
  });

  it("reports duplicate repo names instead of writing them", () => {
    setLang("en");
    const errors: string[] = [];
    const { target, instance, view } = mountView(
      parseConfig({ base: { view: { project_icons: { shop: "~/a.png" } } } })!, undefined, errors,
    );
    try {
      const field = iconsField(target);
      addRow(field);
      const inputs = Array.from(field.querySelectorAll<HTMLInputElement>(".row input"));
      type(inputs[2], "shop");
      type(inputs[3], "~/b.png");
      expect(errors.some((e) => e.includes("duplicate"))).toBe(true);
      expect(view().project_icons).toEqual({ shop: "~/a.png" });
    } finally { unmount(instance); }
  });

  it("writes profile-only project icons without touching the base", () => {
    setLang("cs");
    const payload = parseConfig({
      base: { view: { project_icons: { api: "~/a.png" } } },
      profiles: { night: { view: {} } },
    })!;
    const { props, target, instance, view } = mountView(payload, "night");
    try {
      const field = iconsField(target);
      expect(field.textContent).toContain("api → ~/a.png");
      addRow(field);
      const [repo, path] = Array.from(field.querySelectorAll<HTMLInputElement>(".row input"));
      type(repo, "web");
      type(path, "~/w.png");
      expect((props.payload as ConfigPayload).profiles.night.view).toEqual({ project_icons: { web: "~/w.png" } });
      expect(view().project_icons).toEqual({ api: "~/a.png" });
      expect(field.querySelector<HTMLButtonElement>("button.remove")!.title).toBe("Odebrat ikonu projektu");
    } finally { unmount(instance); }
  });
});
