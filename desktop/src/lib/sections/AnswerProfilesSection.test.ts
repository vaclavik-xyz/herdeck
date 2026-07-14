import { describe, expect, it } from "vitest";
import { flushSync, mount, unmount } from "svelte";

import { parseConfig } from "../configClient";
import { setLang } from "../i18n.svelte";
import AnswerProfilesSection from "./AnswerProfilesSection.svelte";

describe("AnswerProfilesSection", () => {
  it("does not offer deletion for backend built-ins", () => {
    setLang("en");
    const payload = parseConfig({
      base: {
        answer_profiles: {
          constructor: { approve: ["a"], deny: ["d"], stop: ["s"] },
        },
      },
    })!;
    const target = document.createElement("div");
    document.body.appendChild(target);
    const instance = mount(AnswerProfilesSection, {
      target,
      props: {
        payload,
        reloadRev: 0,
        onChange: () => {},
        onError: () => {},
      },
    });
    try {
      const legends = Array.from(target.querySelectorAll("legend"));
      for (const name of ["claude", "codex", "default"]) {
        const legend = legends.find((item) => item.textContent?.trim() === name);
        expect(legend).toBeTruthy();
        expect(legend?.querySelector("button")).toBeNull();
        const fieldset = legend?.closest("fieldset");
        const labels = Array.from(fieldset?.querySelectorAll(".fieldlabel") ?? []);
        expect(labels.some((label) => label.textContent?.trim() === "name")).toBe(false);
      }
      const custom = legends.find((item) => item.textContent?.includes("constructor"));
      expect(custom?.querySelector('button[title="Remove answer profile"]')).toBeTruthy();
      const customLabels = Array.from(custom?.closest("fieldset")?.querySelectorAll(".fieldlabel") ?? []);
      expect(customLabels.some((label) => label.textContent?.trim() === "name")).toBe(true);
    } finally {
      unmount(instance);
      target.remove();
    }
  });

  it("rejects renaming a custom profile to a built-in identity", () => {
    setLang("en");
    const errors: string[] = [];
    const payload = parseConfig({
      base: {
        answer_profiles: {
          custom: { approve: ["a"], deny: ["d"], stop: ["s"] },
        },
      },
    })!;
    const target = document.createElement("div");
    document.body.appendChild(target);
    const instance = mount(AnswerProfilesSection, {
      target,
      props: {
        payload,
        reloadRev: 0,
        onChange: () => {},
        onError: (message) => errors.push(message),
      },
    });
    try {
      const customLegend = Array.from(target.querySelectorAll("legend"))
        .find((item) => item.textContent?.includes("custom"));
      const nameInput = customLegend?.closest("fieldset")?.querySelector("label.field input") as HTMLInputElement;
      nameInput.focus();
      nameInput.value = "customx";
      nameInput.dispatchEvent(new Event("input", { bubbles: true }));
      flushSync();
      expect(document.activeElement).toBe(nameInput);
      expect(customLegend?.textContent).toContain("customx");

      nameInput.value = "claude";
      nameInput.dispatchEvent(new Event("input", { bubbles: true }));
      flushSync();

      expect(errors).toEqual([expect.stringContaining("built-in")]);
      expect(customLegend?.textContent).toContain("customx");
      expect(customLegend?.querySelector("button")).toBeTruthy();
      const resetInput = customLegend?.closest("fieldset")?.querySelector("label.field input") as HTMLInputElement;
      expect(resetInput.value).toBe("customx");
    } finally {
      unmount(instance);
      target.remove();
    }
  });
});
