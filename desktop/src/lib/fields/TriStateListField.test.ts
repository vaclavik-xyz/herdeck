import { describe, expect, it } from "vitest";
import { mount, unmount } from "svelte";

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

  it("preserves an explicitly empty effective seed", () => {
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
      expect(changes).toEqual([["custom", []]]);
    } finally {
      unmount(instance);
    }
  });
});
