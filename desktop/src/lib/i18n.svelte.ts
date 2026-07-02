// Desktop UI language core. The app renders in `locale.lang` ("en" default);
// the value follows the deck's `[view].language` config key — DeckView applies
// it from /state, the config editor from the loaded payload — so the window,
// the editor and the PNG-rendered tiles always speak the same language.
//
// Message shape: shared widget/app strings live in MESSAGES here; each larger
// component keeps a local `defineMessages({...})` const; field tooltips live
// in help.ts. In every case the en/cs key sets are type-locked together.
import { FIELD_HELP, type SectionHelp } from "./help";

export type Lang = "en" | "cs";

export const locale = $state<{ lang: Lang }>({ lang: "en" });

export function setLang(lang: Lang): void {
  locale.lang = lang;
}

/** Coerce a config/state value into a supported language (unknown → "en"). */
export function langOf(v: unknown): Lang {
  return v === "cs" ? "cs" : "en";
}

/** Type-locked per-component catalog: `cs` must carry exactly `en`'s keys. */
export function defineMessages<T extends Record<string, string>>(m: { en: T; cs: T }): {
  en: T;
  cs: T;
} {
  return m;
}

/** Field tooltips for one editor section in the CURRENT language. */
export function fieldHelp(section: string): SectionHelp {
  return FIELD_HELP[locale.lang][section] ?? {};
}

/** Fill `{name}` placeholders in a catalog string. */
export function fmt(template: string, vars: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
}

const MESSAGES = defineMessages({
  en: {
    // field widgets
    "widget.add": "+ add",
    "widget.default": "Default",
    "widget.custom": "Custom",
    "widget.off": "Off",
    "widget.inherit": "Inherit",
    "widget.inherited": "inherited:",
    "widget.default_prefix": "default:",
    "widget.default_empty": "(default)",
    "widget.empty_off": "empty — off",
    "widget.set": "set",
    "widget.clear": "clear",
    "widget.save_keychain": "Save to keychain",
    "widget.cancel": "Cancel",
    "widget.token_value": "token value",
    "widget.remove_row": "Remove row",
  },
  cs: {
    "widget.add": "+ přidat",
    "widget.default": "Výchozí",
    "widget.custom": "Vlastní",
    "widget.off": "Vypnuto",
    "widget.inherit": "Zdědit",
    "widget.inherited": "zděděno:",
    "widget.default_prefix": "výchozí:",
    "widget.default_empty": "(výchozí)",
    "widget.empty_off": "prázdné — vypnuto",
    "widget.set": "nastav",
    "widget.clear": "smazat",
    "widget.save_keychain": "Uložit do keychain",
    "widget.cancel": "Zrušit",
    "widget.token_value": "hodnota tokenu",
    "widget.remove_row": "Odebrat řádek",
  },
});

export type MsgKey = keyof typeof MESSAGES.en;

/** Shared-catalog lookup in the current language (reactive via `locale`). */
export function t(key: MsgKey): string {
  return MESSAGES[locale.lang][key];
}
