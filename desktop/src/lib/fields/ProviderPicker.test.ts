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

  it("keeps custom and known provider ordering when editing", () => {
    const target = document.createElement("div");
    const changes: string[][] = [];
    const instance = mount(ProviderPicker, {
      target,
      props: {
        providers: ["zai", "claude"],
        onchange: (providers: string[]) => changes.push(providers),
      },
    });
    try {
      const input = target.querySelector(".other-row input") as HTMLInputElement;
      input.value = "zai-pro";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      expect(changes).toEqual([["zai-pro", "claude"]]);
    } finally {
      unmount(instance);
    }
  });

  it("does not consume a known-provider prefix while a custom id is typed", () => {
    const target = document.createElement("div");
    const changes: string[][] = [];
    const instance = mount(ProviderPicker, {
      target,
      props: {
        providers: ["custom"],
        onchange: (providers: string[]) => changes.push(providers),
      },
    });
    try {
      const input = target.querySelector(".other-row input") as HTMLInputElement;
      input.value = "claude-enterprise";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      expect(changes).toEqual([]);
      input.dispatchEvent(new Event("change", { bubbles: true }));
      expect(changes).toEqual([["claude-enterprise"]]);
    } finally {
      unmount(instance);
    }
  });
});
