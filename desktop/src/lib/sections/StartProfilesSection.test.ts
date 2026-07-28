import { describe, it, expect } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import StartProfilesSection from "./StartProfilesSection.svelte";
import { parseConfig, DEFAULT_START_PROFILES, type ConfigPayload } from "../configClient";
import { setLang } from "../i18n.svelte";

function payloadWithoutLaunchers(): ConfigPayload {
  const p = parseConfig({
    base: { servers: [{ id: "m4", url: "ws://host:8788", token_env: "T" }] },
    profiles: {},
    local: {},
    secrets: {},
  });
  if (p == null) throw new Error("fixture failed to parse");
  return p;
}

describe("StartProfilesSection default mode", () => {
  it("lists the built-in launchers instead of only describing them", () => {
    setLang("en");
    const target = document.createElement("div");
    document.body.appendChild(target);
    const instance = mount(StartProfilesSection, {
      target,
      props: { payload: payloadWithoutLaunchers(), onChange: () => {}, onError: () => {}, reloadRev: 0 },
    });
    try {
      flushSync();
      const text = target.textContent ?? "";
      for (const name of Object.keys(DEFAULT_START_PROFILES)) {
        expect(text, `default launcher '${name}' not shown`).toContain(name);
      }
      const first = Object.values(DEFAULT_START_PROFILES)[0];
      expect(text).toContain(first.join(" "));
    } finally {
      unmount(instance);
      target.remove();
    }
  });
});
