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
