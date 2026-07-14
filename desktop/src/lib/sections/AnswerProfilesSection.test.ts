import { describe, expect, it } from "vitest";
import { mount, unmount } from "svelte";

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
    }
  });
});
