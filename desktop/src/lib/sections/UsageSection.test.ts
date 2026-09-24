import { describe, expect, it } from "vitest";
import { flushSync, mount, unmount } from "svelte";

import { getAt, parseConfig } from "../configClient";
import UsageSection from "./UsageSection.svelte";

function inputFor(target: HTMLElement, label: string): HTMLInputElement {
  const fieldLabel = target.querySelector<HTMLElement>(`[data-config-key="${label}"]`);
  const input = fieldLabel?.parentElement?.querySelector("input");
  if (!(input instanceof HTMLInputElement)) throw new Error(`missing input for ${label}`);
  return input;
}

describe("UsageSection", () => {
  it("shows every effective backend path default", () => {
    const target = document.createElement("div");
    const instance = mount(UsageSection, {
      target,
      props: { payload: parseConfig({})!, onChange: () => {}, onError: () => {} },
    });
    try {
      expect(inputFor(target, "refresh_secs").value).toBe("300");
      expect(inputFor(target, "refresh_secs").min).toBe("30");
      expect(inputFor(target, "codex_path").value).toBe("codex");
      expect(inputFor(target, "claude_cache_path").value).toBe("~/.cache/herdeck/claude-usage.json");
      expect(inputFor(target, "codexbar_path").value).toBe("codexbar");
    } finally {
      unmount(instance);
    }
  });

  it("keeps a blank codexbar path as an explicit disabled value", () => {
    let changes = 0;
    const target = document.createElement("div");
    const instance = mount(UsageSection, {
      target,
      props: { payload: parseConfig({})!, onChange: () => { changes += 1; }, onError: () => {} },
    });
    try {
      const input = inputFor(target, "codexbar_path");
      input.value = "";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      flushSync();
      expect(inputFor(target, "codexbar_path").value).toBe("");
      expect(changes).toBe(1);
    } finally {
      unmount(instance);
    }
  });

  it("writes alert_at only when the typed list is valid", () => {
    let payload = parseConfig({})!;
    const target = document.createElement("div");
    const instance = mount(UsageSection, {
      target,
      props: {
        get payload() { return payload; },
        set payload(v) { payload = v; },
        onChange: () => {},
        onError: () => {},
      },
    });
    try {
      const input = inputFor(target, "alert_at");
      expect(input.value).toBe("");
      input.value = "95, 80";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      flushSync();
      expect(getAt(payload, "base", "usage", "alert_at")).toEqual([95, 80]);
      input.value = "95, 180";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      flushSync();
      expect(getAt(payload, "base", "usage", "alert_at")).toEqual([95, 80]);
      input.value = "";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      flushSync();
      expect(getAt(payload, "base", "usage", "alert_at")).toBeUndefined();
    } finally {
      unmount(instance);
    }
  });

  it("shows inherited path defaults in profile overlays", () => {
    const target = document.createElement("div");
    const instance = mount(UsageSection, {
      target,
      props: {
        payload: parseConfig({ profiles: { night: {} } })!,
        editProfile: "night",
        onChange: () => {},
        onError: () => {},
      },
    });
    try {
      const hints = Array.from(target.querySelectorAll(".override"), (field) => ({
        label: field.querySelector<HTMLElement>("[data-config-key]")?.dataset.configKey,
        hint: field.querySelector(".hint")?.textContent,
      }));
      expect(hints.find((x) => x.label === "codex_path")?.hint).toContain("codex");
      expect(hints.find((x) => x.label === "claude_cache_path")?.hint).toContain("claude-usage.json");
    } finally {
      unmount(instance);
    }
  });
});
