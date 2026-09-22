// Keys the running runtime reads only at startup (see their "Takes effect after
// restart" tooltips in help.ts and the consumers in src/herdeck/): a live
// config reload cannot apply them, so Apply must not just say "saved".
import type { ConfigPayload } from "./configClient";

/** [local] keys of the machine-local config that need a runtime restart. */
export const RESTART_REQUIRED_LOCAL_KEYS = ["deck", "herdr_socket", "web_bind", "web_port"] as const;

function localValue(payload: ConfigPayload | null, key: string): unknown {
  const local = (payload?.local as Record<string, unknown> | undefined)?.local;
  if (local == null || typeof local !== "object") return undefined;
  return (local as Record<string, unknown>)[key];
}

/** The restart-only keys whose value differs between the last applied config
 *  and the one just saved, in declaration order. Empty/absent compare equal
 *  (both mean "default"), so clearing an already-empty field is not a change. */
export function restartRequiredChanges(
  before: ConfigPayload | null,
  after: ConfigPayload | null,
): string[] {
  const norm = (v: unknown): string => (v == null || v === "" ? "" : JSON.stringify(v));
  return RESTART_REQUIRED_LOCAL_KEYS.filter(
    (key) => norm(localValue(before, key)) !== norm(localValue(after, key)),
  );
}
