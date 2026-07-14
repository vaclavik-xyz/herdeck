import { describe, expect, it } from "vitest";
import { flushSync, mount, unmount } from "svelte";

import TriStateListField from "./TriStateListField.svelte";

describe("TriStateListField", () => {
  it("seeds Custom with the effective default list", () => {
    const changes: Array<[string, string[]]> = [];
    const target = document.createElement("div");
    const instance = mount(TriStateListField, {
      target,
      props: {
        label: "require_confirm_for",
        state: "default",
        list: [],
        customSeed: ["act_force"],
        onchange: (state, list) => changes.push([state, list]),
      },
    });
    try {
      (target.querySelectorAll("button")[1] as HTMLButtonElement).click();
      expect(changes).toEqual([["custom", ["act_force"]]]);
    } finally {
      unmount(instance);
    }
  });

  it("keeps Off distinct from the backend default", () => {
    const changes: Array<[string, string[]]> = [];
    const target = document.createElement("div");
    const instance = mount(TriStateListField, {
      target,
      props: {
        label: "require_confirm_for",
        state: "default",
        list: ["act_force"],
        customSeed: ["act_force"],
        onchange: (state, list) => changes.push([state, list]),
      },
    });
    try {
      (target.querySelectorAll("button")[2] as HTMLButtonElement).click();
      expect(changes).toEqual([["empty", ["act_force"]]]);
    } finally {
      unmount(instance);
    }
  });

  it("opens an editable draft for an empty effective seed", () => {
    const changes: Array<[string, string[]]> = [];
    const target = document.createElement("div");
    const instance = mount(TriStateListField, {
      target,
      props: {
        label: "tile_primary",
        state: "default",
        list: [],
        customSeed: [],
        onchange: (state, list) => changes.push([state, list]),
      },
    });
    try {
      (target.querySelectorAll("button")[1] as HTMLButtonElement).click();
      flushSync();
      expect(changes).toEqual([]);
      const input = target.querySelector("input") as HTMLInputElement;
      expect(input.value).toBe("");
      input.value = "repo";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      flushSync();
      expect(changes).toEqual([["custom", ["repo"]]]);
    } finally {
      unmount(instance);
    }
  });
});
