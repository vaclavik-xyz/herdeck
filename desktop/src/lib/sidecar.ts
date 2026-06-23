// Pure helpers for the sidecar discovery + health data. Framework-free so they
// are unit-testable under Vitest without a Tauri WebView.
//
// NOTE: the WebView does NOT fetch the sidecar directly — that would be a
// cross-origin request the loopback sidecar (owned by the sidecar slice) does
// not send CORS headers for. Instead the Rust shell exposes `get_discovery` and
// `check_health` commands; the frontend `invoke`s those and shapes the results
// with the helpers below.

/** What the WebView is told about the sidecar (the Rust `DiscoveryView`). The
 *  access token is intentionally NOT here — the shell proxies sidecar access
 *  through the `check_health` command, so JS never holds it. */
export interface Discovery {
  url: string;
  host: string;
  port: number;
  source: string;
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
