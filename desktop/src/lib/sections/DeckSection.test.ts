import { describe, expect, it } from "vitest";
import { flushSync, mount, unmount } from "svelte";

import { parseConfig } from "../configClient";
import DeckSection from "./DeckSection.svelte";

function inputFor(target: HTMLElement, label: string): HTMLInputElement {
  const fieldLabel = Array.from(target.querySelectorAll(".fieldlabel")).find(
    (node) => node.textContent?.trim() === label,
  );
  const input = fieldLabel?.parentElement?.querySelector("input");
  if (!(input instanceof HTMLInputElement)) throw new Error(`missing input for ${label}`);
  return input;
}

describe("DeckSection", () => {
  it("shows effective grid and local hardware defaults", () => {
    const target = document.createElement("div");
    const instance = mount(DeckSection, {
      target,
      props: { payload: parseConfig({})!, onChange: () => {}, onError: () => {} },
    });
    try {
      expect(inputFor(target, "grid").value).toBe("5x3");
      expect(inputFor(target, "brightness").value).toBe("80");
      expect(inputFor(target, "debounce").value).toBe("0.25");
      expect(inputFor(target, "keep_alive_interval").value).toBe("5");
      expect(inputFor(target, "tick_interval").value).toBe("0.4");

      expect(inputFor(target, "web_port").max).toBe("65535");
      expect(inputFor(target, "brightness").max).toBe("100");
      expect(inputFor(target, "debounce").max).toBe("60");
      expect(inputFor(target, "keep_alive_interval").max).toBe("86400");
      expect(inputFor(target, "debounce").validity.stepMismatch).toBe(false);
      expect(inputFor(target, "keep_alive_interval").validity.stepMismatch).toBe(false);
      expect(inputFor(target, "tick_interval").validity.stepMismatch).toBe(false);
    } finally {
      unmount(instance);
    }
  });

  it("shows and seeds the inherited grid default in an overlay", () => {
    const target = document.createElement("div");
    const instance = mount(DeckSection, {
      target,
      props: {
        payload: parseConfig({ profiles: { night: {} } })!,
        editProfile: "night",
        onChange: () => {},
        onError: () => {},
      },
    });
    try {
      expect(target.querySelector(".override .hint")?.textContent).toContain("5x3");
      (target.querySelector(".override .seg button:nth-child(2)") as HTMLButtonElement).click();
      flushSync();
      expect((target.querySelector(".override input") as HTMLInputElement).value).toBe("5x3");
    } finally {
      unmount(instance);
    }
  });
});
