// en/cs sentences for the health notices (app window rows, deck status dot)
// and the action toasts. One short human sentence per problem; the raw
// backend facts stay in HealthProblem.detail (title=) and in Maintenance.
import { defineMessages, fmt } from "./i18n.svelte";
import type { HealthAction, HealthProblem } from "./healthStatus";

export const NOTICE_MESSAGES = defineMessages({
  en: {
    config_error: "The config does not load — the deck shows an error instead of agents",
    runtime_mismatch: "Runtime {runtime} differs from the app {app} — restart the runtime",
    bridge_protocol: "Bridge {id} speaks a newer protocol than this runtime — update the runtime",
    bridge_mismatch: "Bridge {id} runs {bridge}, the runtime {runtime} — update the bridge",
    bridge_newer: "Bridge {id} runs {bridge}, newer than the runtime {runtime} — update the app",
    bridge_token: "Bridge {id} rejected the token{since}",
    bridge_down: "Bridge {id} is disconnected{since}",
    d200_down: "The D200 is disconnected{since}",
    d200_locked: "Another runtime (pid {pid}) holds the D200",
    app_update: "Herdeck {version} is available",
    since: " ({ago})",
    seconds: "{n} s",
    minutes: "{n} min",
    hours: "{n} h",
    days: "{n} d",
    // actions
    update_bridge: "Update bridge",
    restart_deck: "Restart deck",
    restart_runtime: "Restart runtime",
    fix_config: "Fix config…",
    install_update: "Install and restart",
    installing: "Installing…",
    working: "Working…",
    details: "Details",
    details_title: "Open Settings → Maintenance",
    release_notes: "Release notes",
    dismiss: "Hide until it changes",
    later: "Later",
    // severities (screen readers + the deck dot)
    sev_error: "Error",
    sev_warning: "Warning",
    sev_info: "Info",
    notices_label: "Health notices",
    dot_title_more: "{text} (+{n} more)",
    dot_open: "Open Maintenance",
    // toasts
    toasts_label: "Notifications",
    close: "Close",
    copy_title: "Copy the command to the clipboard",
    step_download: "Download",
    step_install: "Install",
    step_verify: "Verify",
    step_restart: "Restart",
    bridge_updating: "Updating bridge {id}…",
    bridge_updated: "Bridge {id} is updated to {version}",
    deck_restarting: "Restarting the deck…",
    runtime_restarting: "Restarting the runtime…",
    runtime_restarted: "The runtime was restarted",
    runtime_restart_failed: "Restarting the runtime failed",
    update_checking: "Checking for updates…",
    update_up_to_date: "Herdeck is up to date.",
    update_failed: "Update check failed: {reason}",
    update_install_failed: "Installing the update failed: {reason}",
  },
  cs: {
    config_error: "Config nejde načíst — deck místo agentů ukazuje chybu",
    runtime_mismatch: "Runtime {runtime} se liší od aplikace {app} — restartuj runtime",
    bridge_protocol: "Bridge {id} mluví novějším protokolem než tento runtime — aktualizuj runtime",
    bridge_mismatch: "Bridge {id} běží ve verzi {bridge}, runtime v {runtime} — aktualizuj bridge",
    bridge_newer: "Bridge {id} běží ve verzi {bridge}, novější než runtime {runtime} — aktualizuj aplikaci",
    bridge_token: "Bridge {id} odmítl token{since}",
    bridge_down: "Bridge {id} je odpojený{since}",
    d200_down: "D200 je odpojený{since}",
    d200_locked: "D200 drží jiný runtime (pid {pid})",
    app_update: "Je dostupný Herdeck {version}",
    since: " ({ago})",
    seconds: "{n} s",
    minutes: "{n} min",
    hours: "{n} h",
    days: "{n} d",
    update_bridge: "Aktualizovat bridge",
    restart_deck: "Restartovat deck",
    restart_runtime: "Restartovat runtime",
    fix_config: "Opravit config…",
    install_update: "Nainstalovat a restartovat",
    installing: "Instaluji…",
    working: "Pracuji…",
    details: "Podrobnosti",
    details_title: "Otevřít Nastavení → Údržba",
    release_notes: "Poznámky k vydání",
    dismiss: "Skrýt, dokud se to nezmění",
    later: "Později",
    sev_error: "Chyba",
    sev_warning: "Varování",
    sev_info: "Informace",
    notices_label: "Upozornění na stav",
    dot_title_more: "{text} (+{n} další)",
    dot_open: "Otevřít Údržbu",
    toasts_label: "Oznámení",
    close: "Zavřít",
    copy_title: "Zkopírovat příkaz do schránky",
    step_download: "Stažení",
    step_install: "Instalace",
    step_verify: "Ověření",
    step_restart: "Restart",
    bridge_updating: "Aktualizuji bridge {id}…",
    bridge_updated: "Bridge {id} je aktualizovaný na {version}",
    deck_restarting: "Restartuji deck…",
    runtime_restarting: "Restartuji runtime…",
    runtime_restarted: "Runtime byl restartován",
    runtime_restart_failed: "Restart runtime selhal",
    update_checking: "Kontroluji aktualizace…",
    update_up_to_date: "Herdeck je aktuální.",
    update_failed: "Kontrola aktualizací selhala: {reason}",
    update_install_failed: "Instalace aktualizace selhala: {reason}",
  },
});

export type NoticeMessages = typeof NOTICE_MESSAGES.en;

/** "3 min" — how long ago `sinceMs` was. */
export function agoText(sinceMs: number, now: number, m: NoticeMessages): string {
  const s = Math.max(0, Math.round((now - sinceMs) / 1000));
  if (s < 60) return fmt(m.seconds, { n: s });
  if (s < 3600) return fmt(m.minutes, { n: Math.round(s / 60) });
  if (s < 86400) return fmt(m.hours, { n: Math.round(s / 3600) });
  return fmt(m.days, { n: Math.round(s / 86400) });
}

/** The problem as one human sentence in the current language. */
export function problemText(p: HealthProblem, m: NoticeMessages, now: number = Date.now()): string {
  const since = p.sinceMs === null ? "" : fmt(m.since, { ago: agoText(p.sinceMs, now, m) });
  return fmt(m[p.kind], { ...p.vars, since });
}

/** The primary button's label for `a`. */
export function actionLabel(a: HealthAction, m: NoticeMessages): string {
  switch (a.kind) {
    case "update_bridge": return m.update_bridge;
    case "restart_deck": return m.restart_deck;
    case "restart_runtime": return m.restart_runtime;
    case "fix_config": return m.fix_config;
  }
}
