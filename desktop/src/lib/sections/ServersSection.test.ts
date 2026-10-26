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

  it("shows the trimmed label in the server header", () => {
    const view = mountWith([{ id: "m4", url: "ws://x", token_env: "T", label: "  M4  " }]);
    try {
      expect(view.target.querySelector("legend")?.textContent?.trim()).toMatch(/^M4 · m4/);
    } finally {
      unmount(view.instance);
    }
  });

  for (const lang of ["en", "cs"] as const) {
    it(`flags a label over 16 characters inline (${lang})`, () => {
      setLang(lang);
      const view = mountWith([
        { id: "ok", url: "ws://x", token_env: "T", label: "  " + "x".repeat(16) + "  " }, // trimmed: exactly the limit
        { id: "m4", url: "ws://y", token_env: "T", label: "y".repeat(17) },
      ]);
      try {
        expect(view.target.querySelectorAll("[data-label-issue]").length).toBe(1);
        const note = view.target.querySelector("[data-label-issue=too_long]");
        expect(note?.closest("fieldset")?.querySelector("legend")?.textContent).toMatch(/· m4/);
        expect(note?.textContent).toBe(
          lang === "en" ? "label is longer than 16 characters" : "label je delší než 16 znaků",
        );
      } finally {
        unmount(view.instance);
        setLang("en");
      }
    });

    it(`flags a duplicate label inline, case-insensitively (${lang})`, () => {
      setLang(lang);
      const view = mountWith([
        { id: "a", url: "ws://x", token_env: "T", label: "M4" },
        { id: "b", url: "ws://y", token_env: "T", label: " m4 " },
      ]);
      try {
        const notes = view.target.querySelectorAll("[data-label-issue=duplicate]");
        expect(notes.length).toBe(1);
        expect(notes[0].closest("fieldset")?.querySelector("legend")?.textContent).toMatch(/^m4 · b/);
        expect(notes[0].textContent).toBe(
          lang === "en" ? "another server already uses the label 'm4'" : "label 'm4' už používá jiný server",
        );
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
