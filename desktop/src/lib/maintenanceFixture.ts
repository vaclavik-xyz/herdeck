// A representative GET /maintenance body (as relayed, with the shell's `app`)
// for the maintenance tests.
export function rawStatus(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    version: "0.9.1",
    pid: 4242,
    uptime_s: 3600,
    process: { frozen: true, executable: "/x", spawned_by_app: false, is_service: true },
    service: {
      installed: true,
      label: "dev.herdeck.runtime",
      unit_path: "/Users/me/Library/LaunchAgents/dev.herdeck.runtime.plist",
      program: "/Applications/herdeck.app/Contents/Resources/herdeck-deckapp/herdeck-deckapp",
      from_app: true,
    },
    logs: { runtime: "/Users/me/Library/Logs/herdeck-runtime.log", app: "/Users/me/Library/Logs/herdeck/herdeck.log" },
    d200: {
      connected: true, since: 1, last_frame_at: 1000, last_error: null, lock_owner: null,
      supervised: true, state: "connected", usb_present: true, usb_location: "20-1:2",
      power_cycle: { available: true, reason: null, uhubctl: "/opt/homebrew/bin/uhubctl", hub: "20-1", port: 2, source: "last_seen" },
    },
    servers: {
      m4: { managed: true, connected: true, bridge_version: "0.9.0", protocol: 3, last_error: null, ever_connected: true },
    },
    app: {
      version: "0.9.1", channel: "stable", bundle: "/Applications/herdeck.app",
      bundled_runtime: "/Applications/herdeck.app/Contents/Resources/herdeck-deckapp/herdeck-deckapp",
      spawned_runtime: false,
    },
    ...over,
  };
}
