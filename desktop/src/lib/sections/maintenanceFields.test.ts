// The config keys the Maintenance feature added to the editor:
// [hardware].d200_standard_writer / uhubctl / usb_hub / usb_port (DeckSection),
// (ServersSection no longer offers desktop_read_state) and
// [hotkeys].restart_deck (DesktopSection).
import { describe, expect, it } from "vitest";
import { flushSync, mount, unmount } from "svelte";

import { FIELD_HELP } from "../help";
import { getAt, parseConfig, serversOf, type ConfigPayload } from "../configClient";
import DeckSection from "./DeckSection.svelte";
import DesktopSection from "./DesktopSection.svelte";
import ServersSection from "./ServersSection.svelte";

function field(target: HTMLElement, key: string): { input: HTMLInputElement; title: string | null } {
  const label = target.querySelector<HTMLElement>(`[data-config-key="${key}"]`);
  const input = label?.parentElement?.querySelector("input");
  if (!(input instanceof HTMLInputElement)) throw new Error(`missing input for ${key}`);
  return { input, title: label!.getAttribute("title") };
}

function mountSection(component: unknown, payload: ConfigPayload) {
  const target = document.createElement("div");
  document.body.appendChild(target); // delegated change events need a connected tree
  const state = { payload };
  const instance = mount(component as never, {
    target,
    props: {
      get payload() { return state.payload; },
      set payload(v: ConfigPayload) { state.payload = v; },
      onChange: () => {},
      onError: () => {},
    },
  });
  flushSync();
  return { target, state, done: () => { unmount(instance); target.remove(); } };
}

describe("DeckSection maintenance keys", () => {
  it("writes the D200 writer and USB power-cycle keys into [hardware]", () => {
    const { target, state, done } = mountSection(DeckSection, parseConfig({})!);
    try {
      const writer = field(target, "d200_standard_writer");
      expect(writer.title).toBe(FIELD_HELP.en.deck.d200_standard_writer);
      writer.input.click();
      flushSync();
      expect(getAt(state.payload, "local", "hardware", "d200_standard_writer")).toBe(true);
      field(target, "d200_standard_writer").input.click();
      flushSync();
      expect(getAt(state.payload, "local", "hardware", "d200_standard_writer")).toBeUndefined();

      const hub = field(target, "usb_hub").input;
      hub.value = "20-1.4";
      hub.dispatchEvent(new Event("input", { bubbles: true }));
      flushSync();
      expect(getAt(state.payload, "local", "hardware", "usb_hub")).toBe("20-1.4");

      const port = field(target, "usb_port").input;
      expect(port.min).toBe("1");
      expect(port.max).toBe("127");
      expect(field(target, "uhubctl").title).toBe(FIELD_HELP.en.deck.uhubctl);
    } finally {
      done();
    }
  });
});

describe("ServersSection desktop_read_state", () => {
  it("is no longer offered on a T3 server, yet the stored key survives", () => {
    const p = parseConfig({
      base: { servers: [{ id: "t3", url: "http://h:3773", token_env: "B", backend: "t3", desktop_read_state: true }] },
    })!;
    const { target, state, done } = mountSection(ServersSection, p);
    try {
      expect(target.querySelectorAll('[data-config-key="desktop_read_state"]')).toHaveLength(0);
      expect(serversOf(state.payload)[0].desktop_read_state).toBe(true);
    } finally {
      done();
    }
  });
});

describe("DesktopSection restart_deck hotkey", () => {
  it("edits [hotkeys].restart_deck", () => {
    const { target, state, done } = mountSection(DesktopSection, parseConfig({})!);
    try {
      const hotkey = field(target, "restart_deck");
      expect(hotkey.input.value).toBe("");
      expect(hotkey.title).toBe(FIELD_HELP.en.desktop.restart_deck);
      hotkey.input.value = "CmdOrCtrl+Alt+R";
      hotkey.input.dispatchEvent(new Event("input", { bubbles: true }));
      flushSync();
      expect(getAt(state.payload, "base", "hotkeys", "restart_deck")).toBe("CmdOrCtrl+Alt+R");
    } finally {
      done();
    }
  });
});
