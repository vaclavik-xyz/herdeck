import { describe, it, expect } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import ColorSwatchField from "./ColorSwatchField.svelte";
import { PALETTE_NAMES } from "../statusColors";

function render(props: Record<string, unknown>) {
  const target = document.createElement("div");
  document.body.appendChild(target);
  const instance = mount(ColorSwatchField, { target, props });
  flushSync();
  return {
    target,
    cleanup: () => { unmount(instance); target.remove(); },
  };
}

describe("ColorSwatchField", () => {
  it("renders one swatch per palette name", () => {
    const { target, cleanup } = render({ label: "working", value: "green", onchange: () => {} });
    try {
      // The "(default)" choice is also role="radio" (it's a genuine member of
      // the mutually-exclusive set), so exclude it by data-color to count only
      // real palette swatches.
      expect(target.querySelectorAll('[role="radio"][data-color]:not([data-color=""])').length).toBe(PALETTE_NAMES.length);
    } finally { cleanup(); }
  });

  it("marks the selected palette name as checked", () => {
    const { target, cleanup } = render({ label: "working", value: "amber", onchange: () => {} });
    try {
      const checked = target.querySelector('[role="radio"][aria-checked="true"]');
      expect(checked?.getAttribute("data-color")).toBe("amber");
    } finally { cleanup(); }
  });

  it("reports the palette NAME (not a css colour) when a swatch is clicked", () => {
    const seen: string[] = [];
    const { target, cleanup } = render({ label: "working", value: "green", onchange: (v: string) => seen.push(v) });
    try {
      target.querySelector<HTMLButtonElement>('[data-color="violet"]')?.click();
      flushSync();
      expect(seen).toEqual(["violet"]);
    } finally { cleanup(); }
  });

  it("surfaces a stored value that is not in the palette instead of hiding it", () => {
    const { target, cleanup } = render({ label: "working", value: "#ff00ff", onchange: () => {} });
    try {
      const unknown = target.querySelector(".unknown");
      expect(unknown?.textContent).toContain("#ff00ff");
    } finally { cleanup(); }
  });

  it("offers the inherit/default choice only when allowEmpty is set", () => {
    const withEmpty = render({ label: "working", value: "", allowEmpty: true, onchange: () => {} });
    try {
      expect(withEmpty.target.querySelector('[data-color=""]')).not.toBeNull();
    } finally { withEmpty.cleanup(); }

    const without = render({ label: "working", value: "green", allowEmpty: false, onchange: () => {} });
    try {
      expect(without.target.querySelector('[data-color=""]')).toBeNull();
    } finally { without.cleanup(); }
  });

  it("keeps the label help contract (fieldlabel + title)", () => {
    const { target, cleanup } = render({ label: "working", value: "green", help: "Tile colour.", onchange: () => {} });
    try {
      const label = target.querySelector(".fieldlabel");
      expect(label?.getAttribute("title")).toBe("Tile colour.");
    } finally { cleanup(); }
  });

  it("exactly one swatch is tabbable at a time", () => {
    const { target, cleanup } = render({ label: "working", value: "amber", onchange: () => {} });
    try {
      expect(target.querySelectorAll('[role="radio"][tabindex="0"]').length).toBe(1);
    } finally { cleanup(); }
  });

  it("makes the first swatch tabbable when the stored value is unknown", () => {
    const { target, cleanup } = render({ label: "working", value: "#ff00ff", onchange: () => {} });
    try {
      const tabbable = target.querySelectorAll('[role="radio"][tabindex="0"]');
      expect(tabbable.length).toBe(1);
      expect(tabbable[0]).toBe(target.querySelector('[role="radio"]'));
    } finally { cleanup(); }
  });

  it("ArrowRight moves from the selected swatch to the next palette name", () => {
    const seen: string[] = [];
    const { target, cleanup } = render({ label: "working", value: "green", allowEmpty: false, onchange: (v: string) => seen.push(v) });
    try {
      const idx = PALETTE_NAMES.indexOf("green");
      const current = target.querySelector<HTMLButtonElement>('[data-color="green"]');
      current?.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", cancelable: true, bubbles: true }));
      expect(seen).toEqual([PALETTE_NAMES[(idx + 1) % PALETTE_NAMES.length]]);
    } finally { cleanup(); }
  });

  it("ArrowLeft from the first swatch wraps to the last", () => {
    const seen: string[] = [];
    const first = PALETTE_NAMES[0];
    const { target, cleanup } = render({ label: "working", value: first, allowEmpty: false, onchange: (v: string) => seen.push(v) });
    try {
      const current = target.querySelector<HTMLButtonElement>(`[data-color="${first}"]`);
      current?.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", cancelable: true, bubbles: true }));
      expect(seen).toEqual([PALETTE_NAMES[PALETTE_NAMES.length - 1]]);
    } finally { cleanup(); }
  });

  it("includes the (default) choice in the arrow-key sequence when allowEmpty is set", () => {
    const seen: string[] = [];
    const last = PALETTE_NAMES[PALETTE_NAMES.length - 1];
    const { target, cleanup } = render({ label: "working", value: last, allowEmpty: true, onchange: (v: string) => seen.push(v) });
    try {
      const current = target.querySelector<HTMLButtonElement>(`[data-color="${last}"]`);
      current?.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", cancelable: true, bubbles: true }));
      expect(seen).toEqual([""]);
    } finally { cleanup(); }
  });

  it("Home and End jump to the first and last palette name", () => {
    const seen: string[] = [];
    const { target, cleanup } = render({ label: "working", value: "amber", allowEmpty: false, onchange: (v: string) => seen.push(v) });
    try {
      const current = target.querySelector<HTMLButtonElement>('[data-color="amber"]');
      current?.dispatchEvent(new KeyboardEvent("keydown", { key: "End", cancelable: true, bubbles: true }));
      current?.dispatchEvent(new KeyboardEvent("keydown", { key: "Home", cancelable: true, bubbles: true }));
      expect(seen).toEqual([PALETTE_NAMES[PALETTE_NAMES.length - 1], PALETTE_NAMES[0]]);
    } finally { cleanup(); }
  });

  it("prevents the default scroll behaviour on arrow-key navigation", () => {
    const { target, cleanup } = render({ label: "working", value: "green", allowEmpty: false, onchange: () => {} });
    try {
      const current = target.querySelector<HTMLButtonElement>('[data-color="green"]');
      const event = new KeyboardEvent("keydown", { key: "ArrowRight", cancelable: true, bubbles: true });
      current?.dispatchEvent(event);
      expect(event.defaultPrevented).toBe(true);
    } finally { cleanup(); }
  });
});
