import type { Lang } from "./i18n.svelte";

const LABELS: Record<Lang, Record<string, string>> = {
  en: {
    id: "Server name", url: "Bridge address", token_env: "Token reference", token_file: "Token file",
    grid: "Grid size", overview_order: "Server order", deck: "Deck type",
    herdr_socket: "Herdr socket", web_bind: "Simulator address", web_port: "Simulator port",
    icons_dir: "Custom icons folder", terminal_app: "Terminal app", brightness: "Display brightness", debounce: "Press debounce",
    keep_alive_interval: "Keep-alive interval", tick_interval: "Refresh interval",
    management: "Controls layout", agent_order: "Agent order", agent_slots: "Agent slots", show_profile_on_panel: "Show active profile", collapse_idle: "Collapse idle agents",
    working_animation: "Working animation", tile_fill: "Tile fill", tile_icon: "Tile icon", project_icons: "Project icons", bottom_row: "Bottom row",
    tile_fields: "Visible tile details", tile_primary: "Primary tile line", tile_secondary: "Secondary tile line",
    language: "Language", working: "Working", idle: "Idle", blocked: "Blocked", done: "Done",
    waiting: "Waiting", unknown: "Unknown", offline: "Offline", server_accents: "Server colors",
    macros: "Macros", label: "Button label", text: "Message", name: "Name", argv: "Launch command",
    enabled: "Notifications", sound: "Sound", on: "Notify for", backends: "Delivery channels",
    sounds_blocked: "Sound: blocked", sounds_done: "Sound: done",
    chat_id: "Chat ID", message_thread_id: "Forum topic ID", interactive: "Interactive controls",
    allowed_user_ids: "Allowed users", prompt_max_chars: "Prompt preview length",
    approve_always: "Approve permanently", require_confirm_for: "Confirm before",
    providers: "Usage providers", paid_only: "Paid accounts only", refresh_secs: "Refresh interval",
    codex_path: "Codex executable", claude_cache_path: "Claude usage cache", codexbar_path: "CodexBar executable",
    alert_at: "Usage alert levels", alert_reset: "Notify on limit reset",
    approve: "Approve keys", deny: "Deny keys", stop: "Stop keys", keys: "Key mappings",
    extends: "Inherits from", servers: "Remote servers", deck_always_on_top: "Deck always on top",
    toggle_deck: "Show or hide shortcut", next_blocked: "Next blocked agent shortcut",
    restart_deck: "Restart deck shortcut", d200_standard_writer: "D200 standard writer",
    uhubctl: "uhubctl executable", usb_hub: "D200 USB hub", usb_port: "D200 USB port",
    desktop_read_state: "Read T3 desktop state",
  },
  cs: {
    id: "Název serveru", url: "Adresa bridge", token_env: "Reference tokenu", token_file: "Soubor s tokenem",
    grid: "Rozměr mřížky", overview_order: "Pořadí serverů", deck: "Typ decku",
    herdr_socket: "Herdr socket", web_bind: "Adresa simulátoru", web_port: "Port simulátoru",
    icons_dir: "Složka vlastních ikon", terminal_app: "Aplikace terminálu", brightness: "Jas displeje", debounce: "Ochrana proti dvojstisku",
    keep_alive_interval: "Interval udržování spojení", tick_interval: "Interval obnovení",
    management: "Rozložení ovládání", agent_order: "Pořadí agentů", agent_slots: "Místa pro agenty", show_profile_on_panel: "Zobrazit aktivní profil", collapse_idle: "Sbalit nečinné agenty",
    working_animation: "Animace při práci", tile_fill: "Výplň dlaždice", tile_icon: "Ikona dlaždice", project_icons: "Ikony projektů", bottom_row: "Spodní řada",
    tile_fields: "Údaje na dlaždici", tile_primary: "První řádek dlaždice", tile_secondary: "Druhý řádek dlaždice",
    language: "Jazyk", working: "Pracuje", idle: "Nečinný", blocked: "Blokovaný", done: "Hotovo",
    waiting: "Čeká", unknown: "Neznámý stav", offline: "Odpojený", server_accents: "Barvy serverů",
    macros: "Makra", label: "Popisek tlačítka", text: "Zpráva", name: "Název", argv: "Spouštěcí příkaz",
    enabled: "Notifikace", sound: "Zvuk", on: "Upozornit při", backends: "Způsob doručení",
    sounds_blocked: "Zvuk: blokovaný", sounds_done: "Zvuk: hotovo",
    chat_id: "ID chatu", message_thread_id: "ID tématu fóra", interactive: "Interaktivní ovládání",
    allowed_user_ids: "Povolení uživatelé", prompt_max_chars: "Délka náhledu promptu",
    approve_always: "Trvalé schválení", require_confirm_for: "Potvrdit před akcí",
    providers: "Poskytovatelé využití", paid_only: "Jen placené účty", refresh_secs: "Interval obnovení",
    codex_path: "Spustitelný soubor Codex", claude_cache_path: "Cache využití Claude",
    codexbar_path: "Spustitelný soubor CodexBar", approve: "Klávesy pro schválení",
    alert_at: "Úrovně upozornění na využití", alert_reset: "Upozornit na obnovu limitu",
    deny: "Klávesy pro zamítnutí", stop: "Klávesy pro zastavení", keys: "Mapování kláves",
    extends: "Dědí z profilu", servers: "Vzdálené servery", deck_always_on_top: "Deck vždy navrchu",
    toggle_deck: "Zkratka pro zobrazení", next_blocked: "Zkratka na dalšího blokovaného",
    restart_deck: "Zkratka pro restart decku", d200_standard_writer: "Standardní zapisovač D200",
    uhubctl: "Spustitelný soubor uhubctl", usb_hub: "USB hub s D200", usb_port: "USB port s D200",
    desktop_read_state: "Číst stav T3 desktopu",
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
