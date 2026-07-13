import { describe, expect, it } from "vitest";
import { mount, unmount } from "svelte";

import ProviderPicker from "./ProviderPicker.svelte";

describe("ProviderPicker", () => {
  it("toggles known providers without dropping custom ids", () => {
    const target = document.createElement("div");
    const changes: string[][] = [];
    const instance = mount(ProviderPicker, {
      target,
      props: {
        providers: ["codex", "zai"],
        onchange: (providers: string[]) => changes.push(providers),
      },
    });
    try {
      (target.querySelector('button[aria-label="Claude"]') as HTMLButtonElement).click();
      expect(changes).toEqual([["codex", "zai", "claude"]]);
    } finally {
      unmount(instance);
    }
  });
});
