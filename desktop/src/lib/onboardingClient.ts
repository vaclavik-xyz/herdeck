// Framework-free helpers for the first-run onboarding card. Like sidecar.ts /
// deckClient.ts, these narrow `unknown` (the raw /setup JSON) and inject the
// Tauri transport, so the whole decision/parse logic is unit-testable under
// Vitest without a Tauri WebView. Onboarding.svelte is a thin template over this.
//
// The access token is NEVER here: the Rust `setup_status` / `setup_connect`
// commands inject it server-side (loopback), exactly like the deck/config
// proxies. A typed remote token flows OUT through `connect` and is never read
// back.

import type { InvokeFn } from "./deckClient";
import { defineMessages, fmt, type Lang } from "./i18n.svelte";

/** Shaped `GET /setup` status (snake_case JSON -> camelCase). `reason` is one of
 *  "mock_env" | "demo" | "first_run" | "local_unavailable" | null. */
export interface SetupStatus {
  mode: string; // "mock" | "local" | "remote" | "mixed"
  connected: boolean;
  reason: string | null;
  localHerdrAvailable: boolean;
  savedRemoteAvailable: boolean;
  choice: string | null; // "local" | "demo" | null
  socketPath: string;
  localSessions: SetupLocalSession[];
  connections: Record<string, boolean>;
}

export interface SetupLocalSession {
  name: string;
  serverId: string;
  socketPath: string;
  available: boolean;
  selected: boolean;
}

/**
 * A discovered session or saved bridge is edited through one connection
 * picker. Rendering quick-connect buttons next to that picker creates two
 * controls with the same outcome and lets their selected state disagree.
 */
export function hasConnectionInventory(status: SetupStatus | null): boolean {
  return (status?.localSessions.length ?? 0) > 0 || status?.savedRemoteAvailable === true;
}

/** Narrow a raw `setup_status` result into a SetupStatus, or null when it is not
 *  a usable status object (treated by the caller as "not ready" -> show the deck). */
export function parseSetupStatus(raw: unknown): SetupStatus | null {
  if (raw == null || typeof raw !== "object") return null;
  const v = raw as Record<string, unknown>;
  if (typeof v.mode !== "string") return null;
  const localSessions: SetupLocalSession[] = [];
  if (Array.isArray(v.local_sessions)) {
    for (const rawSession of v.local_sessions) {
      if (rawSession == null || typeof rawSession !== "object") continue;
      const session = rawSession as Record<string, unknown>;
      if (
        typeof session.name !== "string"
        || typeof session.server_id !== "string"
        || typeof session.socket_path !== "string"
      ) continue;
      localSessions.push({
        name: session.name,
        serverId: session.server_id,
        socketPath: session.socket_path,
        available: session.available === true,
        selected: session.selected === true,
      });
    }
  }
  const connections: Record<string, boolean> = {};
  if (v.connections != null && typeof v.connections === "object" && !Array.isArray(v.connections)) {
    for (const [id, connected] of Object.entries(v.connections as Record<string, unknown>)) {
      connections[id] = connected === true;
    }
  }
  return {
    mode: v.mode,
    connected: v.connected === true,
    reason: typeof v.reason === "string" ? v.reason : null,
    localHerdrAvailable: v.local_herdr_available === true,
    savedRemoteAvailable: v.saved_remote_available === true,
    choice: typeof v.choice === "string" ? v.choice : null,
    socketPath: typeof v.socket_path === "string" ? v.socket_path : "",
    localSessions,
    connections,
  };
}

/** Which surface the deck window should show. */
export type OnboardingView = "deck" | "welcome" | "reconnect";

/** The render decision, EXHAUSTIVE on `reason` and defaulting to the deck so no
 *  setup state can trap the user behind a card that does not apply. */
export function onboardingDecision(status: SetupStatus | null): OnboardingView {
  if (!status) return "deck";
  if (status.reason === "first_run") return "welcome";
  if (status.reason === "local_unavailable") return "reconnect";
  return "deck"; // connected (null), demo, mock_env, or anything unknown
}

/** Fold the manual "change connection" override into the decision: when the user
 *  asked to re-onboard and the status would otherwise show the deck, present the
 *  full welcome card so they can pick a new mode (incl. remote). A genuine
 *  reconnect state still wins (it already needs the card). */
export function shouldOnboard(status: SetupStatus | null, override: boolean): OnboardingView {
  const decision = onboardingDecision(status);
  if (override && decision === "deck") return "welcome";
  return decision;
}

/** The shape POSTed to `setup_connect` as `body`. The remote variant carries the
 *  user-typed token (forwarded by Rust, never read back). */
export type ConnectRequest =
  | { choice: "local" }
  | { choice: "demo" }
  | { choice: "saved" }
  | { choice: "sessions"; sessions: string[]; include_saved: boolean }
  | { choice: "remote"; url: string; token: string; id?: string };

/** Shaped `/setup/connect` result. `ok` gates the flip-to-deck; `error` is the
 *  inline reason on failure (bad_token / unreachable / bad url / a thrown HTTP). */
export interface ConnectResult {
  ok: boolean;
  connected: boolean;
  error: string | null;
  /** Stable snake_case id the runtime adds next to `error` (see
   *  src/herdeck/deckapp for the list); null from an older runtime. */
  code: string | null;
}

/** Narrow a raw connect result; never throws (garbage -> a non-ok result). */
export function parseConnectResult(raw: unknown): ConnectResult {
  if (raw == null || typeof raw !== "object") {
    return { ok: false, connected: false, error: null, code: null };
  }
  const v = raw as Record<string, unknown>;
  return {
    ok: v.ok === true,
    connected: v.connected === true,
    error: typeof v.error === "string" ? v.error : null,
    code: typeof v.code === "string" && v.code !== "" ? v.code : null,
  };
}

/** How the onboarding card talks to the sidecar. Injected so the card stays
 *  framework-free and is testable with a fake, and so the real transport (the
 *  two token-injecting Tauri commands) lives in one place. */
export interface SetupTransport {
  /** `setup_status` -> the parsed status, or null when unavailable/unreadable. */
  status(): Promise<SetupStatus | null>;
  /** `setup_connect({ body })` -> the parsed result. A thrown command (non-200 /
   *  no WebView) becomes a non-ok result carrying the message, so the card can
   *  show it inline rather than crashing. */
  connect(req: ConnectRequest): Promise<ConnectResult>;
}

/** Production transport over the Tauri commands. `setup_status` takes no args;
 *  `setup_connect` takes the request as `body` (matching the Rust signature). */
export function setupTransport(invoke: InvokeFn): SetupTransport {
  return {
    async status() {
      try {
        return parseSetupStatus(await invoke("setup_status"));
      } catch {
        return null;
      }
    },
    async connect(req) {
      try {
        return parseConnectResult(await invoke("setup_connect", { body: req }));
      } catch (e) {
        return { ok: false, connected: false, error: e instanceof Error ? e.message : String(e), code: null };
      }
    },
  };
}

/**
 * Localized, actionable message for a connect failure. The runtime returns a
 * stable machine `code` next to its English `error` sentence; older runtimes
 * only sent the sentence (or a bare probe reason such as 'bad_token'), so the
 * sentence is still matched as a fallback. Unknown codes and strings pass the
 * raw message through verbatim — never hide information.
 */
const CONNECT_ERRORS = defineMessages({
  en: {
    generic: "Connection failed.",
    bad_token: "The token doesn't match. Check the token value on the server (herdeck-bridge).",
    unreachable: "The server is not responding. Check the URL and port (is herdeck-bridge running?).",
    socket_with_path: "herdr socket not found ({path}). Start herdr and try again.",
    socket: "herdr socket not found. Start herdr and try again.",
    local_failed: "Local connection failed. Is herdr running? Try again.",
    snapshot_unsupported: "This herdr is too old for Herdeck. Update herdr and try again.",
    demo_failed: "Switching to demo mode failed.",
    no_saved: "No saved connection found.",
    restore_failed: "The saved connection could not be restored. Try again or reconnect.",
    config_unreadable: "The existing config is unreadable. Fix it in Settings.",
    config_malformed: "The existing config has a broken servers section. Fix it in Settings.",
    config_write_failed: "The config could not be saved. Check that the config folder is writable.",
    config_invalid: "The config was rejected: {detail}",
    no_session_selected: "Select a running local session or include the saved connection.",
    unknown_session: "That local session no longer exists. Refresh and pick again.",
    connections_failed: "The selected connections could not be set up. Try again.",
    token_env_override: "An environment variable overrides the saved token. Unset it or connect with that value.",
    token_env_conflict: "That token name is already used by another server. Pick a different ID.",
    server_not_in_profile: "The active profile does not include this server. Fix it in Settings.",
    remote_failed: "The remote connection could not be set up. Check the URL.",
    token_read_failed: "The existing token could not be read. Check the keychain.",
    token_store_failed: "The token could not be stored. Check the keychain.",
    finalize_failed: "Connected, but finishing setup failed. Try again.",
  },
  cs: {
    generic: "Připojení selhalo.",
    bad_token: "Token nesedí. Zkontroluj hodnotu tokenu na serveru (herdeck-bridge).",
    unreachable: "Server neodpovídá. Zkontroluj URL a port (běží tam herdeck-bridge?).",
    socket_with_path: "herdr socket nenalezen ({path}). Spusť herdr a zkus to znovu.",
    socket: "herdr socket nenalezen. Spusť herdr a zkus to znovu.",
    local_failed: "Lokální připojení selhalo. Běží herdr? Zkus to znovu.",
    snapshot_unsupported: "Tento herdr je pro Herdeck příliš starý. Aktualizuj herdr a zkus to znovu.",
    demo_failed: "Přepnutí do demo režimu selhalo.",
    no_saved: "Uložené spojení nebylo nalezeno.",
    restore_failed: "Uložené spojení se nepodařilo obnovit. Zkus to znovu nebo se připoj znovu.",
    config_unreadable: "Stávající config nejde přečíst. Oprav ho v nastavení.",
    config_malformed: "Stávající config má poškozenou sekci serverů. Oprav ho v nastavení.",
    config_write_failed: "Config se nepodařilo uložit. Zkontroluj, že do složky configu lze zapisovat.",
    config_invalid: "Config byl odmítnut: {detail}",
    no_session_selected: "Vyber běžící lokální session nebo zahrň uložené spojení.",
    unknown_session: "Tahle lokální session už neexistuje. Obnov seznam a vyber znovu.",
    connections_failed: "Vybraná připojení se nepodařilo nastavit. Zkus to znovu.",
    token_env_override: "Proměnná prostředí přebíjí uložený token. Zruš ji, nebo se připoj s její hodnotou.",
    token_env_conflict: "Tento název tokenu už používá jiný server. Zvol jiné ID.",
    server_not_in_profile: "Aktivní profil tento server neobsahuje. Oprav to v nastavení.",
    remote_failed: "Vzdálené připojení se nepodařilo nastavit. Zkontroluj URL.",
    token_read_failed: "Stávající token nejde přečíst. Zkontroluj klíčenku.",
    token_store_failed: "Token se nepodařilo uložit. Zkontroluj klíčenku.",
    finalize_failed: "Připojeno, ale dokončení nastavení selhalo. Zkus to znovu.",
  },
});

type ConnectMessageKey = keyof typeof CONNECT_ERRORS.en;

/** Runtime `code` -> catalog key. Several codes share one message; aliases
 *  cover both the probe reasons (bad_token/unreachable) and the C3 names. */
const CONNECT_CODES: Record<string, ConnectMessageKey> = {
  bad_token: "bad_token",
  unreachable: "unreachable",
  bridge_unreachable: "unreachable",
  socket_not_found: "socket",
  local_start_failed: "local_failed",
  snapshot_unsupported: "snapshot_unsupported",
  demo_failed: "demo_failed",
  no_saved_connection: "no_saved",
  restore_failed: "restore_failed",
  config_unreadable: "config_unreadable",
  config_read_failed: "config_unreadable",
  config_malformed: "config_malformed",
  config_write_failed: "config_write_failed",
  config_save_failed: "config_write_failed",
  config_invalid: "config_invalid",
  validation_failed: "config_invalid",
  no_session_selected: "no_session_selected",
  unknown_session: "unknown_session",
  connections_build_failed: "connections_failed",
  connections_save_failed: "connections_failed",
  token_env_override: "token_env_override",
  // Runtime (SETUP_ERROR_CODES in deckapp/server.py): token_env_conflict = an
  // exported HERDECK_<ID>_TOKEN shadows the typed token (the "override" text);
  // token_env_in_use = the derived env name already belongs to another secret.
  token_env_conflict: "token_env_override",
  token_env_in_use: "token_env_conflict",
  server_not_in_profile: "server_not_in_profile",
  remote_build_failed: "remote_failed",
  token_read_failed: "token_read_failed",
  token_store_failed: "token_store_failed",
  finalize_failed: "finalize_failed",
  // Canonical runtime names (deckapp/server.py SETUP_ERROR_CODES).
  demo_switch_failed: "demo_failed",
  unknown_local_session: "unknown_session",
  selection_save_failed: "connections_failed",
  herdr_socket_not_found: "socket",
  herdr_too_old: "snapshot_unsupported",
  saved_restore_failed: "restore_failed",
  onboarding_finalize_failed: "finalize_failed",
};
// tests/test_deckapp_setup_routes.py pins these keys against the runtime's
// SETUP_ERROR_CODES, so a new runtime code cannot silently fall back to raw text.

export function connectErrorMessage(
  error: string | null | undefined,
  socketPath?: string | null,
  lang: Lang = "en",
  code: string | null = null,
): string {
  const m = CONNECT_ERRORS[lang];
  const key = code ? CONNECT_CODES[code] : undefined;
  if (key === "socket") return socketPath ? fmt(m.socket_with_path, { path: socketPath }) : m.socket;
  if (key === "config_invalid") return error ? fmt(m.config_invalid, { detail: error }) : m.generic;
  if (key) return m[key];
  if (!error) return m.generic;
  if (error === "bad_token") return m.bad_token;
  if (error === "unreachable") return m.unreachable;
  if (error.startsWith("herdr socket not found"))
    return socketPath ? fmt(m.socket_with_path, { path: socketPath }) : m.socket;
  if (error === "could not start local source") return m.local_failed;
  if (error === "could not switch to demo") return m.demo_failed;
  if (error === "no saved connection") return m.no_saved;
  if (error === "existing config is unreadable — fix it in Settings") return m.config_unreadable;
  if (error === "existing config is malformed (servers) — fix it in Settings")
    return m.config_malformed;
  return error;
}

/**
 * Should the card auto-connect to local herdr without a click? True when the
 * user's PERSISTED choice is local (or the card is the reconnect view) and the
 * socket is back — but never during a manual re-onboarding session (`manual`),
 * which is the user's explicit request to change things, and never twice.
 */
export function shouldAutoReconnect(args: {
  view: "welcome" | "reconnect";
  choice: string | null;
  localAvailable: boolean;
  busy: boolean;
  tried: boolean;
  manual: boolean;
}): boolean {
  if (args.manual || args.busy || args.tried) return false;
  if (!args.localAvailable) return false;
  return args.view === "reconnect" || args.choice === "local";
}

/**
 * Delay before an auto-reconnect may fire again after herdr re-appears, by the
 * number of auto attempts already made: 0 for the first, then 2s, 4s, 8s …
 * capped at 60s — so a socket that flaps does not hammer /setup/connect, but a
 * herdr that simply restarted is picked up again without a click.
 */
export function autoReconnectDelayMs(attempts: number): number {
  if (attempts <= 0) return 0;
  return Math.min(60_000, 2_000 * 2 ** (attempts - 1));
}

/** How often the window re-reads /setup: quickly until there is a status and
 *  while an onboarding card is showing (a herdr socket appearing must flip the
 *  card promptly), slowly once the deck is connected — then the poll only has
 *  to notice a dropped connection, and every 2.5s forever was pure waste. */
export function setupPollMs(status: SetupStatus | null, view: OnboardingView): number {
  if (status == null) return 600;
  return view === "deck" ? 20_000 : 2_500;
}
