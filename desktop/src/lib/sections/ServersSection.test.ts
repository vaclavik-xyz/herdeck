import { describe, expect, it } from "vitest";
import { flushSync, mount, unmount } from "svelte";

import { parseConfig } from "../configClient";
import { FIELD_HELP } from "../help";
import { setLang } from "../i18n.svelte";
import ServersSection from "./ServersSection.svelte";

function inputFor(target: HTMLElement, key: string): HTMLInputElement {
  const fieldLabel = target.querySelector<HTMLElement>(`[data-config-key="${key}"]`);
  const input = fieldLabel?.parentElement?.querySelector("input");
  if (!(input instanceof HTMLInputElement)) throw new Error(`missing input for ${key}`);
  return input;
}

describe("ServersSection token_file", () => {
  for (const lang of ["en", "cs"] as const) {
    it(`edits token_file with its ${lang} tooltip`, () => {
      setLang(lang);
      let payload = parseConfig({
        base: { servers: [{ id: "local", url: "ws://x", token_env: "HERDECK_TOKEN" }] },
      })!;
      const target = document.createElement("div");
      const instance = mount(ServersSection, {
        target,
        props: {
          get payload() { return payload; },
          set payload(v) { payload = v; },
          onChange: () => {},
          onError: () => {},
        },
      });
      try {
        flushSync();
        const label = target.querySelector<HTMLElement>('[data-config-key="token_file"]');
        expect(label?.closest(".fieldlabel, [title]")?.getAttribute("title")
          ?? label?.getAttribute("title")).toBe(FIELD_HELP[lang].servers.token_file);
        const input = inputFor(target, "token_file");
        expect(input.value).toBe("");
        input.value = "~/.config/herdeck/local-token";
        input.dispatchEvent(new Event("input", { bubbles: true }));
        flushSync();
        expect(payload.base.servers).toEqual([
          { id: "local", url: "ws://x", token_env: "HERDECK_TOKEN", token_file: "~/.config/herdeck/local-token" },
        ]);
      } finally {
        unmount(instance);
        setLang("en");
      }
    });
  }
});

describe("ServersSection label", () => {
  function mountWith(servers: unknown[]) {
    let payload = parseConfig({ base: { servers } })!;
    const target = document.createElement("div");
    const instance = mount(ServersSection, {
      target,
      props: {
        get payload() { return payload; },
        set payload(v) { payload = v; },
        onChange: () => {},
        onError: () => {},
      },
    });
    flushSync();
    return { target, instance, get payload() { return payload; } };
  }

  for (const lang of ["en", "cs"] as const) {
    it(`edits label with its ${lang} tooltip and drops a blank one`, () => {
      setLang(lang);
      const view = mountWith([{ id: "m4", url: "ws://x", token_env: "T", backend: "t3" }]);
      try {
        const label = view.target.querySelector<HTMLElement>('[data-config-key="label"]');
        expect(label?.closest(".fieldlabel, [title]")?.getAttribute("title")
          ?? label?.getAttribute("title")).toBe(FIELD_HELP[lang].servers.label);
        const input = inputFor(view.target, "label");
        input.value = "M4";
        input.dispatchEvent(new Event("input", { bubbles: true }));
        flushSync();
        expect(view.payload.base.servers).toEqual([
          { id: "m4", url: "ws://x", token_env: "T", backend: "t3", label: "M4" },
        ]);
        input.value = "  ";
        input.dispatchEvent(new Event("input", { bubbles: true }));
        flushSync();
        expect(view.payload.base.servers).toEqual([
          { id: "m4", url: "ws://x", token_env: "T", backend: "t3" },
        ]);
      } finally {
        unmount(view.instance);
        setLang("en");
      }
    });
  }

  it("keeps a stored label when another field is edited", () => {
    const view = mountWith([{ id: "m4", url: "ws://x", token_env: "T", label: "M4" }]);
    try {
      const input = inputFor(view.target, "url");
      input.value = "ws://y";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      flushSync();
      expect(view.payload.base.servers).toEqual([
        { id: "m4", url: "ws://y", token_env: "T", label: "M4" },
      ]);
    } finally {
      unmount(view.instance);
    }
  });
});
