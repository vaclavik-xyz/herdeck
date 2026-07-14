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
          custom: { approve: ["a"], deny: ["d"], stop: ["s"] },
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
      }
      const custom = legends.find((item) => item.textContent?.includes("custom"));
      expect(custom?.querySelector('button[title="Remove answer profile"]')).toBeTruthy();
    } finally {
      unmount(instance);
    }
  });
});
