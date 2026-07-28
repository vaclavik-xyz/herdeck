import { describe, it, expect } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import FieldGroup from "./FieldGroup.svelte";

describe("FieldGroup", () => {
  it("renders the title as a heading and the optional description", () => {
    const target = document.createElement("div");
    document.body.appendChild(target);
    const instance = mount(FieldGroup, {
      target,
      props: { title: "Deck layout", description: "How launchers fit the deck." },
    });
    try {
      flushSync();
      expect(target.querySelector("h3")?.textContent).toBe("Deck layout");
      expect(target.querySelector("p")?.textContent).toBe("How launchers fit the deck.");
    } finally {
      unmount(instance);
      target.remove();
    }
  });

  it("omits the description paragraph when none is given", () => {
    const target = document.createElement("div");
    document.body.appendChild(target);
    const instance = mount(FieldGroup, { target, props: { title: "Colors" } });
    try {
      flushSync();
      expect(target.querySelector("p")).toBeNull();
    } finally {
      unmount(instance);
      target.remove();
    }
  });
});
