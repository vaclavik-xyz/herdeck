// en/cs texts for the Maintenance section and the HealthNotice actions, plus
// the pure outcome → text mapping both share (every runtime outcome code of
// deckapp/maintenance.py and bridge_update.py has its own sentence).
import { defineMessages, fmt } from "./i18n.svelte";
import { managedBridgeCommand, type BridgeUpdateView, type D200Status, type DeckOutcome, type RuntimeOrigin } from "./maintenanceClient";

export const MAINTENANCE_MESSAGES = defineMessages({
  en: {
    // overview
    versions: "Versions",
    versions_hint: "App, runtime and every bridge should run the same release.",
    app: "App",
    runtime: "Runtime",
    bridge: "Bridge {id}",
    unknown: "unknown",
    mismatch: "differs from the runtime",
    loading: "Asking the runtime…",
    unreachable: "The runtime does not answer: {error}",
    refresh: "Refresh",
    // runtime
    runtime_heading: "Runtime on this Mac",
    origin_service_this_app: "Runs as a service from this app — it updates together with the app.",
    origin_service_other_app: "Runs as a service from another copy of the app ({program}).",
    origin_service_checkout: "Runs as a service from a source checkout ({program}) — it does not update with the app.",
    origin_self_spawned: "Runs inside this app — it stops when the app quits.",
    origin_attached: "Started outside this app (pid {pid}) — the app only attaches to it.",
    pid_uptime: "pid {pid} · up {uptime}",
    install_service: "Run as a service from this app",
    install_confirm: "Install and start the service?",
    install_hint: "Machine-specific switches belong in the config: d200_standard_writer (Deck) and desktop_read_state (Connections), not in the service unit.",
    install_dev: "The bundled runtime exists only in a packaged app; in a dev build use herdeck-service from the checkout.",
    restart_runtime: "Restart runtime",
    restart_runtime_na: "Restart needs the runtime to run as a service; a runtime inside the app restarts with the app.",
    uninstall_service: "Remove service",
    uninstall_confirm: "Remove the service?",
    replace_service: "Replace the service with this app",
    replace_confirm: "The installed service runs {program}. Installing replaces that unit and drops its settings (--config, --port, --env). Replace it?",
    uninstall_other_confirm: "Remove the service that runs another copy of the app ({program})?",
    restart_not_ours: "The installed service runs {program}, not this app — restart it where it was installed.",
    open_runtime_log: "Open runtime log",
    open_app_log: "Open app log",
    no_runtime_log: "The runtime reports no log file (it logs to the terminal or journal that started it).",
    service_ok: "Done.",
    service_failed: "Failed (exit {code}): {detail}",
    service_timeout: "Did not finish in time: {detail}",
    service_running: "Working…",
    log_failed: "Could not open the log: {detail}",
    confirm: "Confirm",
    cancel: "Cancel",
    // deck
    deck_heading: "Deck (Ulanzi D200)",
    d200_connected: "Connected — last frame {ago} ago.",
    d200_connected_idle: "Connected.",
    d200_not_on_usb: "The D200 is not connected to USB — unplug and replug it (or restart its hub).",
    d200_locked: "Held by another runtime (pid {pid}) — quit that one, then restart the deck.",
    d200_disconnected: "On USB but not open — try Restart deck.",
    d200_disconnected_unknown: "Not open — try Restart deck; if it stays dark, power-cycle its USB port.",
    d200_unsupervised: "This runtime does not drive a D200 ([local].deck is not d200 here).",
    d200_unknown: "The runtime does not report a D200 state.",
    usb_location: "USB port: {location}",
    last_error: "Last error: {error}",
    restart_deck: "Restart deck",
    power_cycle: "Power-cycle USB port",
    power_cycle_uhubctl_missing: "Power-cycle needs uhubctl: brew install uhubctl, or set [hardware].uhubctl.",
    power_cycle_uhubctl_not_executable: "[hardware].uhubctl does not point to an executable file.",
    power_cycle_location_unknown: "The D200's hub port is not known yet: connect it once, or set usb_hub + usb_port (Deck).",
    power_cycle_unavailable: "Power-cycle is not available ({reason}).",
    // deck outcomes
    deck_reopened: "The deck was reopened and redrawn.",
    deck_not_present: "The D200 is not on USB — unplug and replug it (or power-cycle its port).",
    deck_failed: "Reopening the deck failed: {error}",
    deck_locked_by: "The deck is held by another runtime (pid {pid}).",
    deck_timeout: "The deck did not answer in time.",
    deck_busy: "A deck action is already running.",
    deck_unsupported: "This runtime does not drive a D200.",
    deck_cycled: "USB port {hub}:{port} was power-cycled; the deck reconnects in a few seconds.",
    deck_needs_admin: "uhubctl needs admin rights here. Run this in a terminal:",
    deck_cycle_failed: "Power-cycle failed: {error}",
    deck_cycle_timeout: "uhubctl did not finish in time.",
    deck_unavailable: "Power-cycle is not available.",
    deck_http: "The runtime refused the action: {error}",
    deck_unreachable: "The runtime does not answer: {error}",
    copy: "Copy",
    copy_title: "Copy the command to the clipboard",
    copied: "Copied",
    // bridges
    bridges_heading: "Bridges",
    no_bridges: "No remote bridges are configured.",
    connected: "connected",
    disconnected: "disconnected",
    managed: "managed install",
    not_managed: "not a managed install",
    managed_unknown: "install type unknown",
    update_bridge: "Update bridge",
    update_bridge_title: "Ask bridge {id} to install version {version} and restart",
    updating: "Updating…",
    upd_updated: "Updated: {message}",
    upd_pending: "Still running: {message}",
    upd_not_managed: "This bridge was not installed as a managed service, so it cannot update itself. Run once on that machine:",
    upd_readonly: "The bridge refused: this server's token is read-only.",
    upd_failed: "The update failed: {message}",
    upd_busy: "Another update of this bridge is already running.",
    upd_unsupported: "This bridge is too old to update itself; update it by hand once.",
    upd_disconnected: "The server is not connected, so nothing was sent.",
    upd_newer: "The bridge is newer than this runtime — update the app instead.",
    upd_current: "The bridge already runs this version.",
    upd_downgrade: "The bridge refused to go back to an older version.",
    offer_unknown: "Install type unknown (an older bridge, or T3) — update it by hand.",
    offer_unsupported: "This bridge predates self-update; update it by hand once.",
    offer_none: "Up to date.",
    upd_http: "The runtime refused the update: {message}",
    upd_unreachable: "The runtime does not answer: {message}",
    upd_other: "{code}: {message}",
    // health notice
    open_maintenance: "Open Maintenance",
  },
  cs: {
    versions: "Verze",
    versions_hint: "Aplikace, runtime i každý bridge by měly běžet ve stejné verzi.",
    app: "Aplikace",
    runtime: "Runtime",
    bridge: "Bridge {id}",
    unknown: "neznámá",
    mismatch: "liší se od runtime",
    loading: "Ptám se runtime…",
    unreachable: "Runtime neodpovídá: {error}",
    refresh: "Obnovit",
    runtime_heading: "Runtime na tomto Macu",
    origin_service_this_app: "Běží jako služba z této aplikace — aktualizuje se spolu s ní.",
    origin_service_other_app: "Běží jako služba z jiné kopie aplikace ({program}).",
    origin_service_checkout: "Běží jako služba ze zdrojového checkoutu ({program}) — s aplikací se neaktualizuje.",
    origin_self_spawned: "Běží uvnitř této aplikace — skončí, když aplikaci ukončíš.",
    origin_attached: "Spuštěný mimo tuto aplikaci (pid {pid}) — aplikace se k němu jen připojuje.",
    pid_uptime: "pid {pid} · běží {uptime}",
    install_service: "Spouštět jako službu z této aplikace",
    install_confirm: "Nainstalovat a spustit službu?",
    install_hint: "Přepínače pro konkrétní stroj patří do configu: d200_standard_writer (Deck) a desktop_read_state (Připojení), ne do unitu služby.",
    install_dev: "Přibalený runtime existuje jen v zabalené aplikaci; ve vývojovém buildu použij herdeck-service z checkoutu.",
    restart_runtime: "Restartovat runtime",
    restart_runtime_na: "Restart vyžaduje runtime běžící jako služba; runtime uvnitř aplikace se restartuje s aplikací.",
    uninstall_service: "Odebrat službu",
    uninstall_confirm: "Odebrat službu?",
    replace_service: "Nahradit službu touto aplikací",
    replace_confirm: "Nainstalovaná služba spouští {program}. Instalace ten unit nahradí a zahodí jeho nastavení (--config, --port, --env). Nahradit?",
    uninstall_other_confirm: "Odebrat službu, která spouští jinou kopii aplikace ({program})?",
    restart_not_ours: "Nainstalovaná služba spouští {program}, ne tuto aplikaci — restartuj ji tam, kde byla nainstalována.",
    open_runtime_log: "Otevřít log runtime",
    open_app_log: "Otevřít log aplikace",
    no_runtime_log: "Runtime nehlásí žádný log soubor (loguje do terminálu nebo journalu, který ho spustil).",
    service_ok: "Hotovo.",
    service_failed: "Selhalo (exit {code}): {detail}",
    service_timeout: "Nedokončilo se včas: {detail}",
    service_running: "Pracuji…",
    log_failed: "Log se nepodařilo otevřít: {detail}",
    confirm: "Potvrdit",
    cancel: "Zrušit",
    deck_heading: "Deck (Ulanzi D200)",
    d200_connected: "Připojeno — poslední snímek před {ago}.",
    d200_connected_idle: "Připojeno.",
    d200_not_on_usb: "D200 není připojený k USB — odpoj ho a znovu zapoj (nebo restartuj jeho hub).",
    d200_locked: "Drží ho jiný runtime (pid {pid}) — ukonči ho a pak restartuj deck.",
    d200_disconnected: "Je na USB, ale není otevřený — zkus Restartovat deck.",
    d200_disconnected_unknown: "Není otevřený — zkus Restartovat deck; když zůstane tmavý, restartuj napájení jeho USB portu.",
    d200_unsupervised: "Tento runtime D200 neovládá ([local].deck tu není d200).",
    d200_unknown: "Runtime nehlásí stav D200.",
    usb_location: "USB port: {location}",
    last_error: "Poslední chyba: {error}",
    restart_deck: "Restartovat deck",
    power_cycle: "Restartovat napájení USB portu",
    power_cycle_uhubctl_missing: "Restart napájení potřebuje uhubctl: brew install uhubctl, nebo nastav [hardware].uhubctl.",
    power_cycle_uhubctl_not_executable: "[hardware].uhubctl neukazuje na spustitelný soubor.",
    power_cycle_location_unknown: "Port hubu s D200 zatím není známý: jednou ho připoj, nebo nastav usb_hub + usb_port (Deck).",
    power_cycle_unavailable: "Restart napájení není dostupný ({reason}).",
    deck_reopened: "Deck byl znovu otevřen a překreslen.",
    deck_not_present: "D200 není na USB — odpoj ho a znovu zapoj (nebo restartuj napájení portu).",
    deck_failed: "Znovuotevření decku selhalo: {error}",
    deck_locked_by: "Deck drží jiný runtime (pid {pid}).",
    deck_timeout: "Deck neodpověděl včas.",
    deck_busy: "Akce s deckem už běží.",
    deck_unsupported: "Tento runtime D200 neovládá.",
    deck_cycled: "Napájení USB portu {hub}:{port} bylo restartováno; deck se za pár sekund připojí.",
    deck_needs_admin: "uhubctl tu potřebuje práva správce. Spusť v terminálu:",
    deck_cycle_failed: "Restart napájení selhal: {error}",
    deck_cycle_timeout: "uhubctl nedoběhl včas.",
    deck_unavailable: "Restart napájení není dostupný.",
    deck_http: "Runtime akci odmítl: {error}",
    deck_unreachable: "Runtime neodpovídá: {error}",
    copy: "Kopírovat",
    copy_title: "Zkopírovat příkaz do schránky",
    copied: "Zkopírováno",
    bridges_heading: "Bridge",
    no_bridges: "Nejsou nastavené žádné vzdálené bridge.",
    connected: "připojeno",
    disconnected: "odpojeno",
    managed: "spravovaná instalace",
    not_managed: "není spravovaná instalace",
    managed_unknown: "typ instalace neznámý",
    update_bridge: "Aktualizovat bridge",
    update_bridge_title: "Požádat bridge {id} o instalaci verze {version} a restart",
    updating: "Aktualizuji…",
    upd_updated: "Aktualizováno: {message}",
    upd_pending: "Stále běží: {message}",
    upd_not_managed: "Tento bridge nebyl nainstalován jako spravovaná služba, takže se neumí aktualizovat sám. Jednou na tom stroji spusť:",
    upd_readonly: "Bridge odmítl: token tohoto serveru je jen pro čtení.",
    upd_failed: "Aktualizace selhala: {message}",
    upd_busy: "Jiná aktualizace tohoto bridge už běží.",
    upd_unsupported: "Tento bridge je příliš starý na to, aby se aktualizoval sám; jednou ho aktualizuj ručně.",
    upd_disconnected: "Server není připojený, nic se neodeslalo.",
    upd_newer: "Bridge je novější než tento runtime — aktualizuj raději aplikaci.",
    upd_current: "Bridge už tuto verzi má.",
    upd_downgrade: "Bridge odmítl přejít na starší verzi.",
    offer_unknown: "Typ instalace neznámý (starší bridge nebo T3) — aktualizuj ho ručně.",
    offer_unsupported: "Tento bridge je starší než samoaktualizace; jednou ho aktualizuj ručně.",
    offer_none: "Aktuální.",
    upd_http: "Runtime aktualizaci odmítl: {message}",
    upd_unreachable: "Runtime neodpovídá: {message}",
    upd_other: "{code}: {message}",
    open_maintenance: "Otevřít Údržbu",
  },
});

export type MaintenanceMessages = typeof MAINTENANCE_MESSAGES.en;

/** A deck action's outcome as one sentence; `command` is set when the UI
 *  should show a copyable command below it (needs_admin). */
export function deckOutcomeText(o: DeckOutcome, m: MaintenanceMessages): { text: string; command: string | null; ok: boolean } {
  const error = o.error ?? "";
  const plain = (text: string) => ({ text, command: null, ok: o.ok });
  switch (o.outcome) {
    case "reopened": return plain(m.deck_reopened);
    case "not_present": return plain(m.deck_not_present);
    case "locked_by": return plain(fmt(m.deck_locked_by, { pid: o.pid ?? "?" }));
    case "timeout": return plain(o.command ? m.deck_cycle_timeout : m.deck_timeout);
    case "busy": return plain(m.deck_busy);
    case "unsupported": return plain(m.deck_unsupported);
    case "cycled": return plain(fmt(m.deck_cycled, { hub: o.hub ?? "?", port: o.port ?? "?" }));
    case "needs_admin": return { text: m.deck_needs_admin, command: o.command, ok: false };
    case "failed": return plain(fmt(o.command ? m.deck_cycle_failed : m.deck_failed, { error }));
    case "unavailable": return plain(o.reason ? powerCycleReasonText(o.reason, m) : m.deck_unavailable);
    case "unreachable": return plain(fmt(m.deck_unreachable, { error }));
    default: return plain(fmt(m.deck_http, { error: error || o.outcome }));
  }
}

export function powerCycleReasonText(reason: string, m: MaintenanceMessages): string {
  switch (reason) {
    case "uhubctl_missing": return m.power_cycle_uhubctl_missing;
    case "uhubctl_not_executable": return m.power_cycle_uhubctl_not_executable;
    case "location_unknown": return m.power_cycle_location_unknown;
    default: return fmt(m.power_cycle_unavailable, { reason });
  }
}

/** A bridge update's current state as one sentence (+ a copyable command for
 *  not_managed). */
export function bridgeUpdateText(v: BridgeUpdateView, m: MaintenanceMessages): { text: string; command: string | null } {
  const message = v.message;
  const plain = (text: string) => ({ text, command: null });
  switch (v.code) {
    case "updated": return plain(fmt(m.upd_updated, { message }));
    case "pending": return plain(fmt(m.upd_pending, { message }));
    case "not_managed": return { text: m.upd_not_managed, command: managedBridgeCommand(v.target) };
    case "downgrade": return plain(m.upd_downgrade);
    case "readonly": return plain(m.upd_readonly);
    case "failed": return plain(fmt(m.upd_failed, { message }));
    case "busy": return plain(m.upd_busy);
    case "unsupported": return plain(m.upd_unsupported);
    case "disconnected": return plain(m.upd_disconnected);
    case "newer": return plain(m.upd_newer);
    case "current": return plain(m.upd_current);
    case "http": return plain(fmt(m.upd_http, { message }));
    case "unreachable": return plain(fmt(m.upd_unreachable, { message }));
    default: return plain(fmt(m.upd_other, { code: v.code, message }));
  }
}

function ago(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) return `${Math.round(s / 3600)} h`;
  return `${Math.round(s / 86400)} d`;
}

export { ago as durationText };

/** The D200's state as the human sentence the section shows. */
export function d200StateText(d: D200Status, m: MaintenanceMessages, now: number = Date.now()): string {
  switch (d.state) {
    case "connected":
      return d.lastFrameAt != null ? fmt(m.d200_connected, { ago: ago(now - d.lastFrameAt) }) : m.d200_connected_idle;
    case "not_on_usb": return m.d200_not_on_usb;
    case "locked": return fmt(m.d200_locked, { pid: d.lockOwner ?? "?" });
    case "disconnected": return d.usbPresent === true ? m.d200_disconnected : m.d200_disconnected_unknown;
    case "unsupervised": return m.d200_unsupervised;
    default: return m.d200_unknown;
  }
}

export function originText(origin: RuntimeOrigin, program: string | null, pid: number | null, m: MaintenanceMessages): string {
  switch (origin) {
    case "service_this_app": return m.origin_service_this_app;
    case "service_other_app": return fmt(m.origin_service_other_app, { program: program ?? "?" });
    case "service_checkout": return fmt(m.origin_service_checkout, { program: program ?? "?" });
    case "self_spawned": return m.origin_self_spawned;
    default: return fmt(m.origin_attached, { pid: pid ?? "?" });
  }
}
