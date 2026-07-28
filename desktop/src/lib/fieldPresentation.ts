import type { Lang } from "./i18n.svelte";

const LABELS: Record<Lang, Record<string, string>> = {
  en: {
    id: "Server name", url: "Bridge address", token_env: "Token reference",
    grid: "Grid size", overview_order: "Server order", deck: "Deck type",
    herdr_socket: "Herdr socket", web_bind: "Simulator address", web_port: "Simulator port",
    icons_dir: "Custom icons folder", brightness: "Display brightness", debounce: "Press debounce",
    keep_alive_interval: "Keep-alive interval", tick_interval: "Refresh interval",
    management: "Launcher layout", agent_slots: "Agent slots", show_profile_on_panel: "Show active profile",
    working_animation: "Working animation", tile_fill: "Tile fill", bottom_row: "Bottom row",
    tile_fields: "Visible tile details", tile_primary: "Primary tile line", tile_secondary: "Secondary tile line",
    language: "Language", working: "Working", idle: "Idle", blocked: "Blocked", done: "Done",
    waiting: "Waiting", unknown: "Unknown", offline: "Offline", server_accents: "Server colors",
    macros: "Macros", label: "Button label", text: "Message", name: "Name", argv: "Launch command",
    enabled: "Notifications", sound: "Sound", on: "Notify for", backends: "Delivery channels",
    chat_id: "Chat ID", message_thread_id: "Forum topic ID", interactive: "Interactive controls",
    allowed_user_ids: "Allowed users", prompt_max_chars: "Prompt preview length",
    approve_always: "Approve permanently", require_confirm_for: "Confirm before",
    providers: "Usage providers", paid_only: "Paid accounts only", refresh_secs: "Refresh interval",
    codex_path: "Codex executable", claude_cache_path: "Claude usage cache", codexbar_path: "CodexBar executable",
    approve: "Approve keys", deny: "Deny keys", stop: "Stop keys", keys: "Key mappings",
    extends: "Inherits from", servers: "Remote servers", deck_always_on_top: "Deck always on top",
    toggle_deck: "Show or hide shortcut",
  },
  cs: {
    id: "Název serveru", url: "Adresa bridge", token_env: "Reference tokenu",
    grid: "Rozměr mřížky", overview_order: "Pořadí serverů", deck: "Typ decku",
    herdr_socket: "Herdr socket", web_bind: "Adresa simulátoru", web_port: "Port simulátoru",
    icons_dir: "Složka vlastních ikon", brightness: "Jas displeje", debounce: "Ochrana proti dvojstisku",
    keep_alive_interval: "Interval udržování spojení", tick_interval: "Interval obnovení",
    management: "Rozložení spouštěčů", agent_slots: "Místa pro agenty", show_profile_on_panel: "Zobrazit aktivní profil",
    working_animation: "Animace při práci", tile_fill: "Výplň dlaždice", bottom_row: "Spodní řada",
    tile_fields: "Údaje na dlaždici", tile_primary: "První řádek dlaždice", tile_secondary: "Druhý řádek dlaždice",
    language: "Jazyk", working: "Pracuje", idle: "Nečinný", blocked: "Blokovaný", done: "Hotovo",
    waiting: "Čeká", unknown: "Neznámý stav", offline: "Odpojený", server_accents: "Barvy serverů",
    macros: "Makra", label: "Popisek tlačítka", text: "Zpráva", name: "Název", argv: "Spouštěcí příkaz",
    enabled: "Notifikace", sound: "Zvuk", on: "Upozornit při", backends: "Způsob doručení",
    chat_id: "ID chatu", message_thread_id: "ID tématu fóra", interactive: "Interaktivní ovládání",
    allowed_user_ids: "Povolení uživatelé", prompt_max_chars: "Délka náhledu promptu",
    approve_always: "Trvalé schválení", require_confirm_for: "Potvrdit před akcí",
    providers: "Poskytovatelé využití", paid_only: "Jen placené účty", refresh_secs: "Interval obnovení",
    codex_path: "Spustitelný soubor Codex", claude_cache_path: "Cache využití Claude",
    codexbar_path: "Spustitelný soubor CodexBar", approve: "Klávesy pro schválení",
    deny: "Klávesy pro zamítnutí", stop: "Klávesy pro zastavení", keys: "Mapování kláves",
    extends: "Dědí z profilu", servers: "Vzdálené servery", deck_always_on_top: "Deck vždy navrchu",
    toggle_deck: "Zkratka pro zobrazení",
  },
};

export interface FieldPresentation {
  label: string;
  configKey: string | null;
  status: string | null;
}

export function fieldPresentation(rawLabel: string, lang: Lang): FieldPresentation {
  const match = rawLabel.match(/^([a-z][a-z0-9_]*)(?:\s+\(([^)]+)\))?$/);
  const key = match?.[1] ?? "";
  const label = LABELS[lang][key];
  return label
    ? { label, configKey: key, status: match?.[2] ?? null }
    : { label: rawLabel, configKey: null, status: null };
}
