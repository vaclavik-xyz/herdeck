import { describe, expect, it } from "vitest";
import { mount, unmount } from "svelte";

import ProviderPicker from "./ProviderPicker.svelte";

function mountPicker(providers: string[], changes: string[][]) {
  const target = document.createElement("div");
  const instance = mount(ProviderPicker, {
    target,
    props: {
      providers,
      onchange: (next: string[]) => changes.push(next),
    },
  });
  return { target, instance };
}

describe("ProviderPicker", () => {
  it("enables a known provider without dropping custom ids", () => {
    const changes: string[][] = [];
    const { target, instance } = mountPicker(["codex", "zai"], changes);
    try {
      (target.querySelector('button[aria-label="Claude"]') as HTMLButtonElement).click();
      expect(changes).toEqual([["codex", "zai", "claude"]]);
    } finally {
      unmount(instance);
    }
  });

  it("disables only the selected provider and preserves list order", () => {
    const changes: string[][] = [];
    const { target, instance } = mountPicker(["x", "claude", "y", "codex"], changes);
    try {
      (target.querySelector('button[aria-label="Claude"]') as HTMLButtonElement).click();
      expect(changes).toEqual([["x", "y", "codex"]]);
    } finally {
      unmount(instance);
    }
  });
});
