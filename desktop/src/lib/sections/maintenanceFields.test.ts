// The config keys the Maintenance feature added to the editor:
// [hardware].d200_standard_writer / uhubctl / usb_hub / usb_port (DeckSection),
// a T3 server's desktop_read_state (ServersSection) and
// [hotkeys].restart_deck (DesktopSection).
import { describe, expect, it } from "vitest";
import { flushSync, mount, unmount } from "svelte";

import { FIELD_HELP } from "../help";
import { getAt, parseConfig, serversOf, setServerReadState, type ConfigPayload } from "../configClient";
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
  const payload = () => parseConfig({
    base: {
      servers: [
        { id: "m4", url: "ws://h:8788", token_env: "A" },
        { id: "t3", url: "http://h:3773", token_env: "B", backend: "t3" },
      ],
    },
  })!;

  it("is offered only on T3 servers and toggles the key", () => {
    const { target, state, done } = mountSection(ServersSection, payload());
    try {
      const toggles = target.querySelectorAll('[data-config-key="desktop_read_state"]');
      expect(toggles).toHaveLength(1);
      expect(toggles[0].closest("fieldset")?.textContent).toContain("T3 Code");
      field(target, "desktop_read_state").input.click();
      flushSync();
      expect(serversOf(state.payload)[1].desktop_read_state).toBe(true);
      expect(serversOf(state.payload)[0].desktop_read_state).toBeUndefined();
    } finally {
      done();
    }
  });

  it("drops the key when switched off", () => {
    const on = setServerReadState(payload(), 1, true);
    expect(serversOf(on)[1].desktop_read_state).toBe(true);
    const off = setServerReadState(on, 1, false);
    expect("desktop_read_state" in serversOf(off)[1]).toBe(false);
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
