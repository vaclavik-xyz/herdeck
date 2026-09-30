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
import UsageSection from "./UsageSection.svelte";
import AnswerProfilesSection from "./AnswerProfilesSection.svelte";
import ProfilesSection from "./ProfilesSection.svelte";
import DesktopSection from "./DesktopSection.svelte";
import SharedTarget from "./SharedTarget.svelte";
import TelegramBridgeSection from "./TelegramBridgeSection.svelte";
import { parseBridges } from "../bridgeSettings";

// Representative config: at least one entry in every list/map section so the
// per-entry fields (server id/url/token, macro label/text, …) actually render.
function demoPayload(): ConfigPayload {
  const payload = parseConfig({
    base: {
      servers: [
        { id: "m4", url: "ws://host:8788", token_env: "HERDECK_TOKEN_M4" },
        { id: "t3", url: "http://host:3773", token_env: "T3_TOKEN", backend: "t3", desktop_read_state: true },
      ],
      deck: { grid: "5x3", overview_order: ["m4"] },
      view: { management: "launcher_menu", tile_fields: ["repo", "status"], project_icons: { herdeck: "~/icons/herdeck.png" } },
      theme: { colors: { working: "green" }, server_accents: ["teal"] },
      macros: [{ label: "go", text: "continue" }],
      start_profiles: { claude: ["claude"] },
      answer_profiles: {
        claude: { approve: ["1", "enter"], deny: ["esc"], stop: ["ctrl+c"] },
      },
      notifications: { enabled: true, telegram: { token_env: "TG_TOKEN", chat_id: "1" } },
      safety: { approve_always: true, require_confirm_for: ["act_force"] },
      usage: { providers: ["claude"], paid_only: true, refresh_secs: 300, codexbar_path: "codexbar" },
      desktop: { deck_always_on_top: true },
    },
    profiles: { night: { view: { tile_fill: "solid", project_icons: { web: "~/w.png" } } } },
    local: { local: { deck: "d200", web_port: 8800 }, hardware: { brightness: 80 } },
    secrets: {},
  });
  if (payload == null) throw new Error("demo payload failed to parse");
  return payload;
}

type SectionSpec = {
  name: string;
  key: string; // FIELD_HELP section key
  component: unknown;
  overlay: boolean; // supports editProfile
  reloadRev: boolean; // takes reloadRev
  props?: Record<string, unknown>; // extra props (a variant of the section)
};

const SECTIONS: SectionSpec[] = [
  { name: "ServersSection", key: "servers", component: ServersSection, overlay: false, reloadRev: false },
  { name: "DeckSection", key: "deck", component: DeckSection, overlay: true, reloadRev: true },
  { name: "ViewSection", key: "view", component: ViewSection, overlay: true, reloadRev: true },
  { name: "ThemeSection", key: "theme", component: ThemeSection, overlay: true, reloadRev: true },
  { name: "MacrosSection", key: "macros", component: MacrosSection, overlay: true, reloadRev: false },
  { name: "StartProfilesSection", key: "start_profiles", component: StartProfilesSection, overlay: true, reloadRev: true },
  { name: "NotificationsSection", key: "notifications", component: NotificationsSection, overlay: true, reloadRev: true },
  { name: "SafetySection", key: "safety", component: SafetySection, overlay: true, reloadRev: true },
  { name: "UsageSection", key: "usage", component: UsageSection, overlay: true, reloadRev: false },
  { name: "AnswerProfilesSection", key: "answer_profiles", component: AnswerProfilesSection, overlay: true, reloadRev: true },
  { name: "ProfilesSection", key: "profiles", component: ProfilesSection, overlay: false, reloadRev: false },
  { name: "DesktopSection", key: "desktop", component: DesktopSection, overlay: false, reloadRev: false },
  // Bridge shared settings: the rule/alert fields render in base form inside a
  // profile view on an adopted bridge — they need their tooltips there too.
  { name: "NotificationsSection (bridge target)", key: "notifications", component: NotificationsSection, overlay: true, reloadRev: true, props: { sharedOnBridge: true } },
  { name: "UsageSection (bridge target)", key: "usage", component: UsageSection, overlay: true, reloadRev: false, props: { sharedOnBridge: true } },
  {
    name: "SharedTarget", key: "shared", component: SharedTarget, overlay: false, reloadRev: false,
    props: {
      bridges: parseBridges({ m4: { offered: true, connected: true, revision: 1, set: true, source: "bridge", settings: {} }, mb: { offered: true, connected: true, revision: 2, set: true, source: "bridge", settings: {} } }),
      target: "m4", onTarget: () => {}, applyAll: false, onApplyAll: () => {}, editingProfile: true,
      overlayIgnored: [], results: [], baseConfig: {}, put: null, onAdopted: () => {},
    },
  },
  {
    name: "TelegramBridgeSection", key: "telegram_bridge", component: TelegramBridgeSection, overlay: false, reloadRev: false,
    props: {
      bridges: parseBridges({ m4: { offered: false, connected: true, revision: 0, set: false, source: "none", settings: null, telegram: { offered: true, revision: 1, settings: null, status: { token: "file", active: false, inbound: "off", recent_chats: [] } } } }),
      localTelegram: null, call: null, onReload: async () => {}, initialTarget: "m4",
    },
  },
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
  Object.assign(props, spec.props ?? {});
  const instance = mount(spec.component as never, { target, props });
  try {
    flushSync();
    const labels = Array.from(target.querySelectorAll(".fieldlabel"));
    expect(labels.length, `${spec.name}: no fields rendered — fixture broken?`).toBeGreaterThan(0);
    for (const el of labels) {
      const text = el.textContent?.trim() ?? "";
      if (text === "") continue; // inner field wrapped by OverrideField — label lives on the wrapper
      const title = el.getAttribute("title")?.trim() || null;
      const ctx = `${spec.name}${editProfile ? " (overlay)" : ""} [${lang}]: pole "${text}"`;
      expect(title, `${ctx} nemá vysvětlivku (help prop)`).toBeTruthy();
      // When the label is a catalog key (possibly suffixed, e.g. "token (inherited)"),
      // the tooltip must be the catalog text FOR THE MOUNTED LANGUAGE — a stale
      // hardcoded single-language hint would pass a mere non-empty check.
      const bare = text.replace(/ \(.*\)$/, "");
      const expected = FIELD_HELP[lang][spec.key]?.[bare];
      if (expected) expect(title, `${ctx}: tooltip není z katalogu pro '${lang}'`).toBe(expected);
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
