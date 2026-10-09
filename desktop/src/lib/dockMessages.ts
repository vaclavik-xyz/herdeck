// The mini dock's UI strings (en + cs), shared by MiniDock/DockItem/DockCard.
import type { ActionOutcome } from "./agentCardClient";
import type { DockStatus } from "./dockClient";
import { defineMessages, fmt, type Lang } from "./i18n.svelte";

export const DOCK_MESSAGES = defineMessages({
  en: {
    strip_label: "Agents",
    grip_title: "Drag to move the dock",
    status_blocked: "Needs you",
    status_waiting: "Waiting",
    status_done: "Done",
    status_working: "Working",
    status_idle: "Idle",
    status_unknown: "Unknown",
    offline: "Server offline",
    idle_title: "{n} idle agents",
    idle_heading: "Idle agents ({n})",
    more_title: "{n} more agents",
    more_heading: "Other agents ({n})",
    group_label: "{name} · {n} agents · {status}",
    neutral_loading: "Connecting to the runtime…",
    neutral_disconnected: "No server connected — click to open Settings",
    neutral_empty: "No active agents",
    stale_title: "Lost the runtime — showing the last known agents",
    since: "for {t}",
    waiting_on: "waiting on {what}",
    subagents: "{n} subagents running",
    reading: "Reading the prompt…",
    confirm: "Sure? Click again",
    sent: "Sent ✓",
    open_herdr: "Open in herdr",
    close_title: "Close (Esc)",
    detail_gone: "This agent is no longer available.",
    detail_error: "Could not load the details: {e}",
    out_pending: "Sent — the bridge hasn't confirmed yet.",
    out_not_blocked: "The agent is no longer waiting for an answer.",
    out_stale: "The prompt changed — read the new one before answering.",
    out_invalid: "That answer is not offered for this prompt.",
    out_disconnected: "The agent's server is offline.",
    out_readonly: "The bridge accepts only a read-only token here.",
    out_identity_changed: "A different agent now runs in that pane.",
    out_error: "Failed: {message}",
    usage_label: "Usage limits",
    usage_ring: "{name} usage: {windows}",
    usage_left: "{p}% left ({label})",
    usage_used: "{p}% used",
    usage_resets_in: "resets in {t}",
    usage_resets_at: "resets {t}",
    usage_resets_now: "resets now",
    usage_full_early: "runs out ~{t} before reset",
    usage_date: "{d} {mon}, {time}",
    span_lt_min: "< 1 min",
    span_min: "{m} min",
    span_h: "{h} h",
    span_hm: "{h} h {m} min",
  },
  cs: {
    strip_label: "Agenti",
    grip_title: "Táhni pro přesun docku",
    status_blocked: "Potřebuje tě",
    status_waiting: "Čeká",
    status_done: "Hotovo",
    status_working: "Pracuje",
    status_idle: "Nečinný",
    status_unknown: "Neznámý stav",
    offline: "Server je odpojený",
    idle_title: "Nečinní agenti: {n}",
    idle_heading: "Nečinní agenti ({n})",
    more_title: "Další agenti: {n}",
    more_heading: "Ostatní agenti ({n})",
    group_label: "{name} · agentů: {n} · {status}",
    neutral_loading: "Připojuji se k runtime…",
    neutral_disconnected: "Žádný server není připojený — klikni pro Nastavení",
    neutral_empty: "Žádní aktivní agenti",
    stale_title: "Spojení s runtime ztraceno — zobrazuji poslední známé agenty",
    since: "už {t}",
    waiting_on: "čeká na {what}",
    subagents: "Běžící subagenti: {n}",
    reading: "Načítám dotaz…",
    confirm: "Opravdu? Klikni znovu",
    sent: "Odesláno ✓",
    open_herdr: "Otevřít v herdr",
    close_title: "Zavřít (Esc)",
    detail_gone: "Tento agent už není dostupný.",
    detail_error: "Detail se nepodařilo načíst: {e}",
    out_pending: "Odesláno — bridge zatím nepotvrdil.",
    out_not_blocked: "Agent už nečeká na odpověď.",
    out_stale: "Dotaz se změnil — před odpovědí si přečti nový.",
    out_invalid: "Tahle odpověď se k dotazu nenabízí.",
    out_disconnected: "Server agenta je odpojený.",
    out_readonly: "Bridge tu přijímá jen token pro čtení.",
    out_identity_changed: "V tom panelu teď běží jiný agent.",
    out_error: "Nepovedlo se: {message}",
    usage_label: "Limity využití",
    usage_ring: "Využití {name}: {windows}",
    usage_left: "zbývá {p} % ({label})",
    usage_used: "využito {p} %",
    usage_resets_in: "obnoví se za {t}",
    usage_resets_at: "obnoví se {t}",
    usage_resets_now: "obnovuje se",
    usage_full_early: "dojde zhruba {t} před obnovením",
    usage_date: "{d}. {m}., {time}",
    span_lt_min: "< 1 min",
    span_min: "{m} min",
    span_h: "{h} h",
    span_hm: "{h} h {m} min",
  },
});

export function statusLabel(lang: Lang, status: DockStatus, offline = false): string {
  const m = DOCK_MESSAGES[lang];
  if (offline) return m.offline;
  const labels: Record<DockStatus, string> = {
    blocked: m.status_blocked,
    waiting: m.status_waiting,
    done: m.status_done,
    working: m.status_working,
    idle: m.status_idle,
    unknown: m.status_unknown,
  };
  return labels[status];
}

/** What an answer/focus outcome means, for the card's inline note. */
export function outcomeText(lang: Lang, out: ActionOutcome): string {
  const m = DOCK_MESSAGES[lang];
  switch (out.code) {
    case "pending":
      return m.out_pending;
    case "not_blocked":
      return m.out_not_blocked;
    case "stale":
      return m.out_stale;
    case "invalid":
      return m.out_invalid;
    case "disconnected":
      return m.out_disconnected;
    case "readonly":
      return m.out_readonly;
    case "identity_changed":
      return m.out_identity_changed;
    default:
      return fmt(m.out_error, { message: out.message || out.code });
  }
}
