// Mirror of the backend DEFAULT_STATUS_COLORS (src/herdeck/config.py) — keep in
// sync. The config editor uses it as the fallback colour shown/written for a
// status when a profile has no inherited theme.colors.<status> override.
export const DEFAULT_STATUS_COLORS: Record<string, string> = {
  working: "green",
  idle: "blue",
  blocked: "amber",
  done: "cyan",
  unknown: "grey",
  offline: "red",
};
