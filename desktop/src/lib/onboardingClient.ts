// Framework-free helpers for the first-run onboarding card. Like sidecar.ts /
// deckClient.ts, these narrow `unknown` (the raw /setup JSON) and inject the
// Tauri transport, so the whole decision/parse logic is unit-testable under
// Vitest without a Tauri WebView. Onboarding.svelte is a thin template over this.
//
// The access token is NEVER here: the Rust `setup_status` / `setup_connect`
// commands inject it server-side (loopback), exactly like the deck/config
// proxies. A typed remote token flows OUT through `connect` and is never read
// back.

/** Shaped `GET /setup` status (snake_case JSON -> camelCase). `reason` is one of
 *  "mock_env" | "demo" | "first_run" | "local_unavailable" | null. */
export interface SetupStatus {
  mode: string; // "mock" | "local" | "remote"
  connected: boolean;
  reason: string | null;
  localHerdrAvailable: boolean;
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
