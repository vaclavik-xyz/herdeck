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
        "blocked_count": "▲ {n} blocked",
        "needs_you_one": "▲ needs you",
        "needs_you_many": "▲ {n} need you",
        "blocked_for": "blocked {elapsed}",
        "agents_total": "{n} agents",
        "online": "online",
        "usage_title": "usage limits",
        # drill detail (layout.panel_detail)
        "reading_prompt": "reading prompt...",
        "waiting_on": "waiting on: {label}",
        # orchestrator tiles / drill
        "new_agent": "+ New",
        "stop": "Stop",
        "back": "Back",
        "sure": "Sure?",
        "offline_reconnecting": "OFFLINE — reconnecting…",
        "press_to_confirm": "press again to confirm",
        "sent": "sent › {label}",
        # Elgato plugin action keys
        "act.approve": "Approve",
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
        "web.forbidden": "Open the full URL including the ?token=… part from the startup log.",
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
    },
    "cs": {
        "offline_title": "OFFLINE",
        "reconnecting": "připojuji…",
        "blocked_count": "▲ blokováno: {n}",
        "needs_you_one": "▲ čeká na tebe",
        "needs_you_many": "▲ čeká: {n}",
        "blocked_for": "čeká {elapsed}",
        "agents_total": "agentů: {n}",
        "online": "online",
        "usage_title": "limity využití",
        "reading_prompt": "čtu prompt...",
        "waiting_on": "čeká na: {label}",
        "new_agent": "+ Nový",
        "stop": "Stop",
        "back": "Zpět",
        "sure": "Určitě?",
        "offline_reconnecting": "OFFLINE — připojuji…",
        "press_to_confirm": "stiskni znovu pro potvrzení",
        "sent": "posláno › {label}",
        "act.approve": "Schválit",
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
        "status.blocked": "ČEKÁ",
        "status.done": "HOTOVO",
        # blocked already owns "ČEKÁ" (waiting for YOU); this one is passive
        # background work, so it must read differently.
        "status.waiting": "V POZADÍ",
        "status.unknown": "NEZNÁMÝ",
        "status.offline": "OFFLINE",
        "web.press_failed": "stisk selhal — odpojeno?",
        "web.token_expired": "token vypršel — otevři čerstvou URL ze startovacího logu",
        "web.disconnected": "odpojeno — poslední aktualizace před {s} s",
        "web.forbidden": "Otevři celou URL včetně části ?token=… ze startovacího logu.",
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
    },
}


def tr(lang: str, key: str, **fmt: object) -> str:
    """Translate ``key`` into ``lang``, falling back to English for unknown
    languages or keys (a config typo must never crash a render)."""
    table = STRINGS.get(lang, STRINGS["en"])
    text = table.get(key) or STRINGS["en"].get(key) or key
    return text.format(**fmt) if fmt else text
