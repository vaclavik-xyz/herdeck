import { directFetchTransport, type DeckTransport } from "./deckClient";

// Pure helpers for the sidecar discovery + health data. Framework-free so they
// are unit-testable under Vitest without a Tauri WebView.
//
// NOTE: the WebView does NOT fetch the sidecar directly — that would be a
// cross-origin request the loopback sidecar (owned by the sidecar slice) does
// not send CORS headers for. Instead the Rust shell exposes `get_discovery` and
// `check_health` commands; the frontend `invoke`s those and shapes the results
// with the helpers below.

/** What the WebView is told about the sidecar (the Rust `DiscoveryView`).
 *
 *  `token` is optional: slice 3's shell omits it (it proxies `/health` through
 *  the `check_health` command, so JS never needs it). The direct-fetch DeckView
 *  (slice 2) DOES need it to poll `/state` and `POST /press` itself — exactly
 *  the "later direct-fetch DeckView can expose it then" case the Rust
 *  `DiscoveryView` comment foresaw. We read it here when present so the live
 *  deck lights up the moment the shell starts sending it (see `sidecarTransport`
 *  and the open question in the slice report); until then it stays undefined and
 *  the deck shows its offline state. */
export interface Discovery {
  url: string;
  host: string;
  port: number;
  source: string;
  token?: string;
}

/** Shaped `GET /health` result (the bridge token is never exposed — only
 *  `serverId`). */
export interface HealthResult {
  ok: boolean;
  source: string;
  connected: boolean;
  serverId: string | null;
}

/** Narrow an unknown value (e.g. a Tauri `invoke` result) into a Discovery, or
 *  return null. Lets the UI poll the backend and ignore the not-ready-yet case. */
export function asDiscovery(value: unknown): Discovery | null {
  if (value == null || typeof value !== "object") return null;
  const v = value as Record<string, unknown>;
  if (typeof v.url !== "string" || typeof v.source !== "string") {
    return null;
  }
  return {
    url: v.url,
    host: typeof v.host === "string" ? v.host : "",
    port: typeof v.port === "number" ? v.port : 0,
    source: v.source,
    ...(typeof v.token === "string" ? { token: v.token } : {}),
  };
}

/** Shape the raw `/health` JSON (as returned by the Rust `check_health` command)
 *  into a HealthResult, mapping `server_id` -> `serverId`. */
export function parseHealth(raw: unknown): HealthResult {
  const v = (raw ?? {}) as Record<string, unknown>;
  return {
    ok: v.ok === true,
    source: typeof v.source === "string" ? v.source : "unknown",
    connected: v.connected === true,
    serverId: typeof v.server_id === "string" ? v.server_id : null,
  };
}

/** Build the DeckView's live transport from a discovery view, reusing the same
 *  url + token the shell reports (no separate token handling). Returns null when
 *  the url/token aren't available yet — the current slice-3 shell omits the
 *  token, so this is null until the shell starts sending it; the DeckView then
 *  renders its offline state. See the slice report's open question. */
export function sidecarTransport(discovery: Discovery | null): DeckTransport | null {
  if (!discovery || !discovery.url || !discovery.token) return null;
  return directFetchTransport(discovery.url, discovery.token);
}
