// Localized DISPLAY text for config validation issues that carry a stable
// `code` (C3). The English message stays the routing key for
// validationIssues.ts and is kept as the `{detail}` of every template, so a
// translation never hides which key or value the runtime complained about.
// Unknown codes (or none) show the raw message unchanged.
import { defineMessages, fmt, type Lang } from "./i18n.svelte";

const VALIDATION_CODES = defineMessages({
  en: {
    invalid_toml: "The config file is not valid TOML: {detail}",
    invalid_value: "Invalid value: {detail}",
    unknown_key: "Unknown setting: {detail}",
    unknown_server: "Unknown server: {detail}",
    duplicate_server_id: "Duplicate server name: {detail}",
    unknown_profile: "Unknown profile: {detail}",
    profile_cycle: "Profiles inherit from each other in a loop: {detail}",
    stale_revision: "The config changed on disk since it was loaded: {detail}",
  },
  cs: {
    invalid_toml: "Soubor configu není platný TOML: {detail}",
    invalid_value: "Neplatná hodnota: {detail}",
    unknown_key: "Neznámé nastavení: {detail}",
    unknown_server: "Neznámý server: {detail}",
    duplicate_server_id: "Duplicitní název serveru: {detail}",
    unknown_profile: "Neznámý profil: {detail}",
    profile_cycle: "Profily po sobě dědí v kruhu: {detail}",
    stale_revision: "Config se od načtení změnil na disku: {detail}",
  },
});

export function validationMessage(message: string, code: string | undefined, lang: Lang): string {
  const catalog: Record<string, string> = VALIDATION_CODES[lang];
  const template = code ? catalog[code] : undefined;
  return template ? fmt(template, { detail: message }) : message;
}
