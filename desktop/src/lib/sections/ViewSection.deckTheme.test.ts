import { describe, expect, it } from "vitest";
import { flushSync, mount, tick, unmount } from "svelte";

import { parseConfig, type ConfigPayload } from "../configClient";
import { reactiveProps } from "../testProps.svelte";
import { setLang } from "../i18n.svelte";
import { BUILTIN_DECK_THEMES } from "../themesClient";
import ViewSection from "./ViewSection.svelte";

const THEMES = [
  ...BUILTIN_DECK_THEMES.map((id) => ({ id, name: { en: id, cs: id }, builtin: true, error: null })),
  { id: "mine", name: { en: "Mine" }, builtin: false, error: null },
  { id: "broken", name: {}, builtin: false, error: "SyntaxError: bad" },
];

function mountView(payload: ConfigPayload, editProfile?: string, withRuntime = true) {
  const calls: unknown[] = [];
  const invoke = async (cmd: string, args?: Record<string, unknown>) => {
    calls.push([cmd, args]);
    return { status: 200, body: THEMES };
  };
  const props = reactiveProps<Record<string, unknown>>({
    payload, editProfile, onChange: () => {}, onError: () => {}, invoke: withRuntime ? invoke : null,
  });
  const target = document.createElement("div");
  const instance = mount(ViewSection, { target, props: props as never });
  return { props, target, instance, calls };
}

const themeSelect = (target: HTMLElement) =>
  Array.from(target.querySelectorAll("label.field"))
    .find((field) => field.querySelector<HTMLElement>("[data-config-key]")?.dataset.configKey === "deck_theme")!
    .querySelector<HTMLSelectElement>("select")!;
const options = (select: HTMLSelectElement) => Array.from(select.options).map((o) => o.value);
const settle = async () => { for (let i = 0; i < 3; i++) await tick(); flushSync(); };

describe("ViewSection deck_theme", () => {
  it("labels the field 'Deck look' / 'Vzhled decku', never 'Theme'", () => {
    for (const [lang, label] of [["en", "Deck look"], ["cs", "Vzhled decku"]] as const) {
      setLang(lang);
      const { target, instance } = mountView(parseConfig({ base: { view: {} } })!, undefined, false);
      try {
        const field = target.querySelector<HTMLElement>('[data-config-key="deck_theme"]')!;
        expect(field.textContent).toContain(label);
        expect(field.getAttribute("title")).toBeTruthy();
      } finally { unmount(instance); }
    }
  });

  it("offers the built-ins without a runtime", () => {
    setLang("en");
    const { target, instance } = mountView(parseConfig({ base: { view: {} } })!, undefined, false);
    try {
      expect(themeSelect(target).value).toBe(BUILTIN_DECK_THEMES[0]);
      expect(options(themeSelect(target))).toEqual([...BUILTIN_DECK_THEMES]);
    } finally { unmount(instance); }
  });

  it("offers the runtime's loaded themes, reports broken ones and writes the choice", async () => {
    setLang("en");
    const { props, target, instance, calls } = mountView(parseConfig({ base: { view: {} } })!);
    try {
      await settle();
      expect(calls).toEqual([["maintenance_call", { method: "GET", path: "/themes" }]]);
      const select = themeSelect(target);
      expect(options(select)).toEqual([...BUILTIN_DECK_THEMES, "mine"]);
      expect(target.querySelector(".theme-errors")!.textContent).toContain("broken (SyntaxError: bad)");
      flushSync(() => { select.value = "mine"; select.dispatchEvent(new Event("change", { bubbles: true })); });
      expect((props.payload as ConfigPayload).base.view.deck_theme).toBe("mine");
    } finally { unmount(instance); }
  });

  it("overrides the look per profile without touching the base", async () => {
    setLang("en");
    const payload = parseConfig({ base: { view: { deck_theme: "classic" } }, profiles: { night: { view: {} } } })!;
    const { props, target, instance } = mountView(payload, "night");
    try {
      await settle();
      const override = target.querySelector<HTMLElement>('[data-config-key="deck_theme"]')!.closest<HTMLElement>(".override")!;
      flushSync(() => override.querySelectorAll<HTMLButtonElement>(".seg button")[1].click()); // Custom
      const select = override.querySelector<HTMLSelectElement>("select")!;
      expect(options(select)).toEqual([...BUILTIN_DECK_THEMES, "mine"]);
      flushSync(() => { select.value = "mine"; select.dispatchEvent(new Event("change", { bubbles: true })); });
      const next = props.payload as ConfigPayload;
      expect(next.profiles.night.view).toEqual({ deck_theme: "mine" });
      expect(next.base.view.deck_theme).toBe("classic");
    } finally { unmount(instance); }
  });
});
