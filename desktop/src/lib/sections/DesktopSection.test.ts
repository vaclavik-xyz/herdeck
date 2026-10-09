import { afterEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import DesktopSection from "./DesktopSection.svelte";
import { dockPrefs, parseConfig, type ConfigPayload } from "../configClient";
import { setLang } from "../i18n.svelte";
import { reactiveProps } from "../testProps.svelte";

function render(desktop: Record<string, unknown>) {
  const payload = parseConfig({ base: { desktop }, profiles: {}, local: {}, secrets: {} })!;
  const onChange = vi.fn();
  const target = document.createElement("div");
  document.body.appendChild(target);
  const props = reactiveProps({ payload, onChange, onError: () => {} });
  const instance = mount(DesktopSection, { target, props });
  flushSync();
  return {
    target,
    onChange,
    payload: (): ConfigPayload => props.payload,
    cleanup: () => {
      unmount(instance);
      target.remove();
    },
  };
}

afterEach(() => setLang("en"));

describe("DesktopSection dock fields", () => {
  it("shows the dock keys while the floating window is the dock and writes them", () => {
    const r = render({ floating: "dock" });
    try {
      const selects = Array.from(r.target.querySelectorAll<HTMLSelectElement>("select"));
      expect(selects).toHaveLength(2); // floating + dock_show
      const show = selects[1];
      expect(show.value).toBe("active");
      expect(Array.from(show.options).map((o) => o.value)).toEqual(["active", "all", "needs_you"]);
      show.value = "needs_you";
      show.dispatchEvent(new Event("change", { bubbles: true }));
      flushSync();
      const boxes = Array.from(r.target.querySelectorAll<HTMLInputElement>("input[type=checkbox]"));
      expect(boxes).toHaveLength(3); // deck_always_on_top, dock_group, dock_autohide
      expect(boxes[1].checked).toBe(true);
      expect(boxes[2].checked).toBe(false);
      boxes[1].click();
      boxes[2].click();
      flushSync();
      expect(dockPrefs(r.payload())).toEqual({ group: false, show: "needs_you", autohide: true });
      expect(r.onChange).toHaveBeenCalledTimes(3);
    } finally {
      r.cleanup();
    }
  });

  it("hides them for the deck mirror", () => {
    const r = render({ floating: "deck" });
    try {
      expect(r.target.querySelectorAll("select")).toHaveLength(1);
      expect(r.target.querySelectorAll("input[type=checkbox]")).toHaveLength(1);
    } finally {
      r.cleanup();
    }
  });
});
