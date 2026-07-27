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
      expect(target.querySelectorAll('[role="radio"]').length).toBe(PALETTE_NAMES.length);
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
});
