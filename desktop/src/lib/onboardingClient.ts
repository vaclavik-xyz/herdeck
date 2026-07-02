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

/** Shaped `GET /setup` status (snake_case JSON -> camelCase). `reason` is one of
 *  "mock_env" | "demo" | "first_run" | "local_unavailable" | null. */
export interface SetupStatus {
  mode: string; // "mock" | "local" | "remote"
  connected: boolean;
  reason: string | null;
  localHerdrAvailable: boolean;
  savedRemoteAvailable: boolean;
  choice: string | null; // "local" | "demo" | null
  socketPath: string;
}

/** Narrow a raw `setup_status` result into a SetupStatus, or null when it is not
 *  a usable status object (treated by the caller as "not ready" -> show the deck). */
export function parseSetupStatus(raw: unknown): SetupStatus | null {
  if (raw == null || typeof raw !== "object") return null;
  const v = raw as Record<string, unknown>;
  if (typeof v.mode !== "string") return null;
  return {
    mode: v.mode,
    connected: v.connected === true,
    reason: typeof v.reason === "string" ? v.reason : null,
    localHerdrAvailable: v.local_herdr_available === true,
    savedRemoteAvailable: v.saved_remote_available === true,
    choice: typeof v.choice === "string" ? v.choice : null,
    socketPath: typeof v.socket_path === "string" ? v.socket_path : "",
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
  | { choice: "remote"; url: string; token: string; id?: string };

/** Shaped `/setup/connect` result. `ok` gates the flip-to-deck; `error` is the
 *  inline reason on failure (bad_token / unreachable / bad url / a thrown HTTP). */
export interface ConnectResult {
  ok: boolean;
  connected: boolean;
  error: string | null;
}

/** Narrow a raw connect result; never throws (garbage -> a non-ok result). */
export function parseConnectResult(raw: unknown): ConnectResult {
  if (raw == null || typeof raw !== "object") {
    return { ok: false, connected: false, error: null };
  }
  const v = raw as Record<string, unknown>;
  return {
    ok: v.ok === true,
    connected: v.connected === true,
    error: typeof v.error === "string" ? v.error : null,
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
        return { ok: false, connected: false, error: e instanceof Error ? e.message : String(e) };
      }
    },
  };
}

/**
 * Localized, actionable message for a connect failure. The backend returns
 * stable machine codes (probe reasons) or terse English sentences; a Czech
 * first-run card showing raw 'bad_token' gives no guidance. Unknown strings
 * pass through verbatim — never hide information.
 */
const CONNECT_ERRORS: Record<"en" | "cs", Record<string, string>> = {
  en: {
    generic: "Connection failed.",
    bad_token: "The token doesn't match — check the token value on the server (herdeck-bridge).",
    unreachable: "The server is not responding — check the URL and port (is herdeck-bridge running?).",
    socket_with_path: "herdr socket not found ({path}) — start herdr and try again.",
    socket: "herdr socket not found — start herdr and try again.",
    local_failed: "Local connection failed — is herdr running? Try again.",
    demo_failed: "Switching to demo mode failed.",
    no_saved: "No saved connection found.",
    config_unreadable: "The existing config is unreadable — fix it in Settings.",
    config_malformed: "The existing config has a broken servers section — fix it in Settings.",
  },
  cs: {
    generic: "Připojení selhalo.",
    bad_token: "Token nesedí — zkontroluj hodnotu tokenu na serveru (herdeck-bridge).",
    unreachable: "Server neodpovídá — zkontroluj URL a port (běží tam herdeck-bridge?).",
    socket_with_path: "herdr socket nenalezen ({path}) — spusť herdr a zkus to znovu.",
    socket: "herdr socket nenalezen — spusť herdr a zkus to znovu.",
    local_failed: "Lokální připojení selhalo — běží herdr? Zkus to znovu.",
    demo_failed: "Přepnutí do demo režimu selhalo.",
    no_saved: "Uložené spojení nebylo nalezeno.",
    config_unreadable: "Stávající config nejde přečíst — oprav ho v nastavení (Config).",
    config_malformed: "Stávající config má poškozenou sekci serverů — oprav ho v nastavení (Config).",
  },
};

export function connectErrorMessage(
  error: string | null | undefined,
  socketPath?: string | null,
  lang: "en" | "cs" = "en",
): string {
  const m = CONNECT_ERRORS[lang];
  if (!error) return m.generic;
  if (error === "bad_token") return m.bad_token;
  if (error === "unreachable") return m.unreachable;
  if (error.startsWith("herdr socket not found"))
    return socketPath ? m.socket_with_path.replace("{path}", socketPath) : m.socket;
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
