"""User-visible deck-rendering strings, per language.

The deck renderer (tiles + status panel + web simulator) speaks the language
configured as ``[view].language`` ("en" default, "cs"). Only RENDERED text
lives here — config keys, log messages and the CLI stay English. The desktop
window keeps its own catalog (desktop/src/lib/i18n.svelte.ts) because it
renders DOM, not PNGs; keep the two in sync when adding a language.

Texts must stay tile/panel sized: tiles fit ~10 chars of label and one short
status word, panel lines about 18 chars.
"""

from __future__ import annotations

LANGUAGES: tuple[str, ...] = ("en", "cs")

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        # overview panel (layout.panel_overview)
        "offline_title": "OFFLINE",
        "reconnecting": "reconnecting…",
        # config that exists but cannot be loaded (deckapp ConfigErrorSource)
        "config_error_title": "CONFIG ERROR",
        "config_error_token": "no token for '{name}'",
        "config_error_invalid": "config not loadable",
        "config_error_hint": "see Maintenance",
        # partial outage: some, not all, servers down (note line on the calm panel)
        "server_offline": "{name} offline",
        "servers_offline": "{n} servers offline",
        "blocked_count": "▲ {n} blocked",
        "needs_you_one": "▲ needs you",
        "needs_you_many": "▲ {n} need you",
        "blocked_for": "blocked {elapsed}",
        "agents_total": "{n} agents",
        "online": "online",
        "usage_title": "usage limits",
        "usage_meta": "used / reset",
        "usage_reset": "reset",
        # pace projection on a usage detail card (~95 px at 13 px: keep short)
        "usage_pace": "full ~{t} early",
        # drill detail (layout.panel_detail)
        "reading_prompt": "reading prompt...",
        "waiting_on": "waiting on: {label}",
        # orchestrator tiles / drill
        "new_agent": "+ New",
        "stop": "Stop",
        "back": "Back",
        "pin": "Pin here",
        # held status panel (deckapp DeckApp.hold_status_panel)
        "status.reload_failed": "reload failed",
        "status.profile_locked": "profile locked",
        "status.profile_failed": "profile failed",
        "status.pin_failed": "Pin not saved",
        "status.try_again": "Try again",
        "unpin": "Unpin",
        "refresh_title": "Retitle",
        "pinned_absent": "Pinned · offline",
        "pinned_missing": "Pinned · missing",
        "sure": "Sure?",
        "offline_reconnecting": "OFFLINE — reconnecting…",
        "press_to_confirm": "press again to confirm",
        "sent": "sent › {label}",
        "idle_group": "+{n}",
        "idle_group_show": "idle · show",
        "idle_group_hide": "hide",
        "idle_group_hide_sub": "idle",
        "others_blocked": "▲ {n} more blocked",
        "launch_on": "on {server}",
        # notification titles (banner / Telegram headline)
        "notify.title_blocked": "{agent} · needs input",
        "notify.title_done": "{agent} · done",
        # [notifications].remind_after: the agent is still blocked
        "notify.title_reminder": "{agent} · still needs input ({minutes} min)",
        # inline reply field of an actionable blocked banner
        "notify.reply_placeholder": "Reply to the agent…",
        # usage-limit alerts (usage_alerts.usage_alert_message)
        "notify.usage_threshold": "{provider} {window} · {pct} % used",
        "notify.usage_resets_at": "resets {at}",
        "notify.usage_reset_title": "{provider} {window} reset",
        "notify.usage_reset_body": "you can continue",
        # accessible tile descriptions (desktop aria-label via /state)
        "a11y.empty_tile": "empty tile {n}",
        "a11y.pinned": "pinned",
        # Elgato plugin action keys
        "act.approve": "Approve",
        "act.approve_always": "Approve!",
        "act.deny": "Deny",
        "act.stop": "Stop",
        "act.pager": "Next",
        "act.pending": "PENDING",
        "act.stop_confirm": "STOP?",
        # launcher + profile menu
        "new_agent_title": "new agent",
        "pick_type": "pick a type",
        "profiles_entry": "Profiles",
        "profiles_title": "profiles",
        "pick_profile": "pick a profile",
        "locked_by_env": "locked by env",
        "mgmt.notifications": "Notify",
        "mgmt.safety": "Safety",
        "mgmt.theme": "Theme",
        # tile status words (Status.value keyed)
        "status.working": "WORKING",
        "status.idle": "IDLE",
        "status.blocked": "BLOCKED",
        "status.done": "DONE",
        "status.waiting": "WAITING",
        "status.unknown": "UNKNOWN",
        "status.offline": "OFFLINE",
        # web simulator page
        "web.press_failed": "press failed — disconnected?",
        "web.token_expired": "token expired — open the fresh URL from the startup log",
        "web.disconnected": "disconnected — last update {s}s ago",
        "web.forbidden": "Open the full URL including the ?token=… part printed by herdeck-web url.",
        "web.session_required": "Open this deck through its authenticated cockpit.",
        "web.term_no_agent": "no agent terminal on this tile",
        "web.term_disconnected": "bridge disconnected",
        "web.term_ended": "preview ended",
        "web.term_busy": "too many open previews",
        "web.term_close": "close terminal preview",
        "web.term_connecting": "connecting…",
        "web.term_live": "LIVE",
        "web.term_read_only": "READ ONLY",
        "web.term_title": "Live terminal preview",
        "web.term_hint": "Long-press, right-click, or Shift+Enter for a read-only terminal",
        "web.term_connecting_badge": "CONNECTING",
        "web.term_ended_badge": "ENDED",
    },
    "cs": {
        "offline_title": "OFFLINE",
        "reconnecting": "připojuji…",
        "config_error_title": "CHYBA CONFIGU",
        "config_error_token": "chybí token '{name}'",
        "config_error_invalid": "config nejde načíst",
        "config_error_hint": "viz Údržba",
        "server_offline": "{name} offline",
        "servers_offline": "servery offline: {n}",
        "blocked_count": "▲ blokováno: {n}",
        "needs_you_one": "▲ čeká na tebe",
        "needs_you_many": "▲ čeká: {n}",
        "blocked_for": "čeká {elapsed}",
        "agents_total": "agentů: {n}",
        "online": "online",
        "usage_title": "limity využití",
        "usage_meta": "využito / obnova",
        "usage_reset": "obnova",
        "usage_pace": "plno ~{t} dřív",
        "reading_prompt": "čtu prompt...",
        "waiting_on": "čeká na: {label}",
        "new_agent": "+ Nový",
        "stop": "Stop",
        "back": "Zpět",
        "pin": "Připnout",
        "status.reload_failed": "načtení selhalo",
        "status.profile_locked": "profil zamčen",
        "status.profile_failed": "profil selhal",
        "status.pin_failed": "Nepřipnuto",
        "status.try_again": "Zkus znovu",
        "unpin": "Odepnout",
        "refresh_title": "Nový název",
        "pinned_absent": "Připnuto · offline",
        "pinned_missing": "Připnuto · chybí",
        "sure": "Určitě?",
        "offline_reconnecting": "OFFLINE — připojuji…",
        "press_to_confirm": "stiskni znovu pro potvrzení",
        "sent": "posláno › {label}",
        "idle_group": "+{n}",
        "idle_group_show": "nečinní · zobrazit",
        "idle_group_hide": "skrýt",
        "idle_group_hide_sub": "nečinné",
        "others_blocked": "▲ další blokováno: {n}",
        "launch_on": "na {server}",
        "notify.title_blocked": "{agent} · čeká na tebe",
        "notify.title_done": "{agent} · hotovo",
        "notify.title_reminder": "{agent} · pořád čeká na tebe ({minutes} min)",
        "notify.reply_placeholder": "Odpověz agentovi…",
        "notify.usage_threshold": "{provider} {window} · využito {pct} %",
        "notify.usage_resets_at": "obnova {at}",
        "notify.usage_reset_title": "{provider} {window} obnoveno",
        "notify.usage_reset_body": "můžeš pokračovat",
        "a11y.empty_tile": "prázdná dlaždice {n}",
        "a11y.pinned": "připnuto",
        "act.approve": "Schválit",
        "act.approve_always": "Schválit!",
        "act.deny": "Zamítnout",
        "act.stop": "Stop",
        "act.pager": "Další",
        "act.pending": "POSLÁNO",
        "act.stop_confirm": "STOP?",
        "new_agent_title": "nový agent",
        "pick_type": "vyber typ",
        "profiles_entry": "Profily",
        "profiles_title": "profily",
        "pick_profile": "vyber profil",
        "locked_by_env": "zamčeno přes env",
        "mgmt.notifications": "Oznámení",
        "mgmt.safety": "Bezpečí",
        "mgmt.theme": "Barvy",
        "status.working": "PRACUJE",
        "status.idle": "NEČINNÝ",
        # Not "ČEKÁ": next to waiting's "V POZADÍ" (and the panel's "čeká na
        # tebe") a waits-for-you tile and a background-work tile read alike.
        "status.blocked": "BLOKOVÁN",
        "status.done": "HOTOVO",
        # passive background work (CI, review), distinct from blocked
        "status.waiting": "V POZADÍ",
        "status.unknown": "NEZNÁMÝ",
        "status.offline": "OFFLINE",
        "web.press_failed": "stisk selhal — odpojeno?",
        "web.token_expired": "token vypršel — otevři čerstvou URL ze startovacího logu",
        "web.disconnected": "odpojeno — poslední aktualizace před {s} s",
        "web.forbidden": "Otevři celou URL včetně části ?token=… vypsanou příkazem herdeck-web url.",
        "web.session_required": "Otevři tento deck přes přihlášený cockpit.",
        "web.term_no_agent": "na této dlaždici není terminál agenta",
        "web.term_disconnected": "spojení s bridge ztraceno",
        "web.term_ended": "náhled ukončen",
        "web.term_busy": "příliš mnoho otevřených náhledů",
        "web.term_close": "zavřít náhled terminálu",
        "web.term_connecting": "připojuji…",
        "web.term_live": "ŽIVĚ",
        "web.term_read_only": "JEN ČTENÍ",
        "web.term_title": "Živý náhled terminálu",
        "web.term_hint": "Podrž, klikni pravým nebo stiskni Shift+Enter pro náhled terminálu",
        "web.term_connecting_badge": "PŘIPOJUJI",
        "web.term_ended_badge": "UKONČENO",
    },
}


def tr(lang: str, key: str, **fmt: object) -> str:
    """Translate ``key`` into ``lang``, falling back to English for unknown
    languages or keys (a config typo must never crash a render)."""
    table = STRINGS.get(lang, STRINGS["en"])
    text = table.get(key) or STRINGS["en"].get(key) or key
    return text.format(**fmt) if fmt else text
