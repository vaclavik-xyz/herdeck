import { describe, expect, it } from "vitest";
import { flushSync, mount, unmount } from "svelte";

import { parseConfig } from "../configClient";
import { setLang } from "../i18n.svelte";
import { DEFAULT_SERVER_ACCENTS, DEFAULT_STATUS_COLORS } from "../statusColors";
import ThemeSection from "./ThemeSection.svelte";

describe("ThemeSection", () => {
  it("shows the effective backend defaults when theme is absent", () => {
    setLang("en");
    const target = document.createElement("div");
    const instance = mount(ThemeSection, {
      target,
      props: { payload: parseConfig({})!, onChange: () => {}, onError: () => {} },
    });
    try {
      const values = Array.from(target.querySelectorAll(".colors select"), (el) =>
        (el as HTMLSelectElement).value,
      );
      expect(values).toEqual(Object.values(DEFAULT_STATUS_COLORS));
      expect(target.querySelector(".tristate .hint")?.textContent).toContain(
        DEFAULT_SERVER_ACCENTS.join(" · "),
      );
    } finally {
      unmount(instance);
    }
  });

  it("keeps an explicit empty server accent palette as Off", () => {
    let changes = 0;
    const target = document.createElement("div");
    const instance = mount(ThemeSection, {
      target,
      props: { payload: parseConfig({})!, onChange: () => { changes += 1; }, onError: () => {} },
    });
    try {
      const off = target.querySelector(".tristate .seg button:nth-child(3)") as HTMLButtonElement;
      off.click();
      flushSync();
      expect(off.getAttribute("aria-pressed")).toBe("true");
      expect(changes).toBe(1);
    } finally {
      unmount(instance);
    }
  });
});
