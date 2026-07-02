// Enforcement: every labelled field in every config-editor section MUST carry
// a Czech help tooltip (the `help` prop on the field widgets, rendered as
// title= on the label). Mounts each section with a representative payload in
// BOTH base and overlay mode and fails on any label without a title — so a
// newly added field cannot ship without its vysvětlivka.
import { describe, it, expect } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import { FIELD_HELP } from "../help";
import { setLang, type Lang } from "../i18n.svelte";
import { parseConfig, type ConfigPayload } from "../configClient";
import ServersSection from "./ServersSection.svelte";
import DeckSection from "./DeckSection.svelte";
import ViewSection from "./ViewSection.svelte";
import ThemeSection from "./ThemeSection.svelte";
import MacrosSection from "./MacrosSection.svelte";
import StartProfilesSection from "./StartProfilesSection.svelte";
import NotificationsSection from "./NotificationsSection.svelte";
import SafetySection from "./SafetySection.svelte";
import AnswerProfilesSection from "./AnswerProfilesSection.svelte";
import ProfilesSection from "./ProfilesSection.svelte";
import DesktopSection from "./DesktopSection.svelte";

// Representative config: at least one entry in every list/map section so the
// per-entry fields (server id/url/token, macro label/text, …) actually render.
function demoPayload(): ConfigPayload {
  const payload = parseConfig({
    base: {
      servers: [{ id: "m4", url: "ws://host:8788", token_env: "HERDECK_TOKEN_M4" }],
      deck: { grid: "5x3", overview_order: ["m4"] },
      view: { management: "launcher_menu", tile_fields: ["repo", "status"] },
      theme: { colors: { working: "green" }, server_accents: ["teal"] },
      macros: [{ label: "go", text: "continue" }],
      start_profiles: { claude: ["claude"] },
      answer_profiles: {
        claude: { approve: ["1", "enter"], deny: ["esc"], stop: ["ctrl+c"] },
      },
      notifications: { enabled: true, telegram: { token_env: "TG_TOKEN", chat_id: "1" } },
      safety: { approve_always: true, require_confirm_for: ["act_force"] },
      desktop: { window_mode: "floating" },
    },
    profiles: { night: { view: { tile_fill: "solid" } } },
    local: { local: { deck: "d200", web_port: 8800 }, hardware: { brightness: 80 } },
    secrets: {},
  });
  if (payload == null) throw new Error("demo payload failed to parse");
  return payload;
}

type SectionSpec = {
  name: string;
  component: unknown;
  overlay: boolean; // supports editProfile
  reloadRev: boolean; // takes reloadRev
};

const SECTIONS: SectionSpec[] = [
  { name: "ServersSection", component: ServersSection, overlay: false, reloadRev: false },
  { name: "DeckSection", component: DeckSection, overlay: true, reloadRev: false },
  { name: "ViewSection", component: ViewSection, overlay: true, reloadRev: false },
  { name: "ThemeSection", component: ThemeSection, overlay: true, reloadRev: false },
  { name: "MacrosSection", component: MacrosSection, overlay: true, reloadRev: false },
  { name: "StartProfilesSection", component: StartProfilesSection, overlay: true, reloadRev: true },
  { name: "NotificationsSection", component: NotificationsSection, overlay: true, reloadRev: false },
  { name: "SafetySection", component: SafetySection, overlay: true, reloadRev: false },
  { name: "AnswerProfilesSection", component: AnswerProfilesSection, overlay: true, reloadRev: true },
  { name: "ProfilesSection", component: ProfilesSection, overlay: false, reloadRev: false },
  { name: "DesktopSection", component: DesktopSection, overlay: false, reloadRev: false },
];

function assertLabelsHaveHelp(
  spec: SectionSpec,
  editProfile: string | null,
  lang: Lang,
): void {
  setLang(lang);
  const target = document.createElement("div");
  document.body.appendChild(target);
  const props: Record<string, unknown> = {
    payload: demoPayload(),
    onChange: () => {},
    onError: () => {},
  };
  if (spec.reloadRev) props.reloadRev = 0;
  if (spec.overlay) props.editProfile = editProfile;
  const instance = mount(spec.component as never, { target, props });
  try {
    flushSync();
    const labels = Array.from(target.querySelectorAll(".fieldlabel"));
    expect(labels.length, `${spec.name}: no fields rendered — fixture broken?`).toBeGreaterThan(0);
    for (const el of labels) {
      const text = el.textContent?.trim() ?? "";
      if (text === "") continue; // inner field wrapped by OverrideField — label lives on the wrapper
      expect(
        el.getAttribute("title")?.trim() || null,
        `${spec.name}${editProfile ? " (overlay)" : ""}: pole "${text}" nemá vysvětlivku (help prop)`,
      ).toBeTruthy();
    }
  } finally {
    unmount(instance);
    target.remove();
  }
}

describe("config editor help tooltips", () => {
  for (const lang of ["en", "cs"] as const) {
    for (const spec of SECTIONS) {
      it(`${spec.name} [${lang}]: every labelled field has a help tooltip (base mode)`, () => {
        assertLabelsHaveHelp(spec, null, lang);
      });
      if (spec.overlay) {
        it(`${spec.name} [${lang}]: every labelled field has a help tooltip (overlay mode)`, () => {
          assertLabelsHaveHelp(spec, "night", lang);
        });
      }
    }
  }
});

describe("field help catalog parity", () => {
  it("en and cs carry exactly the same sections and field keys", () => {
    expect(Object.keys(FIELD_HELP.cs).sort()).toEqual(Object.keys(FIELD_HELP.en).sort());
    for (const [section, fields] of Object.entries(FIELD_HELP.en)) {
      expect(
        Object.keys(FIELD_HELP.cs[section]).sort(),
        `section '${section}' keys diverge between en and cs`,
      ).toEqual(Object.keys(fields).sort());
    }
  });

  it("every hint is a non-empty single sentence of sane length", () => {
    for (const lang of ["en", "cs"] as const) {
      for (const [section, fields] of Object.entries(FIELD_HELP[lang])) {
        for (const [key, hint] of Object.entries(fields)) {
          expect(hint.trim().length, `${lang}/${section}/${key} empty`).toBeGreaterThan(10);
          expect(hint.length, `${lang}/${section}/${key} too long`).toBeLessThan(140);
        }
      }
    }
  });
});
