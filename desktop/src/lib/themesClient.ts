// Framework-free client for the deck looks ([view].deck_theme) the runtime
// offers: `GET /themes` (src/herdeck/deckapp/server.py -> themes/registry.py),
// relayed by the Rust `maintenance_call` proxy (desktop/src-tauri/src/
// maintenance.rs allow-lists it). The built-ins are offered even without a
// runtime (browser preview, runtime down), so the select never comes up empty.

import type { InvokeFn } from "./deckClient";

/** Built-in deck theme ids in the runtime's order (themes/registry.py
 *  BUILTIN_MODULES). A new built-in theme adds its id here as well. */
export const BUILTIN_DECK_THEMES: readonly string[] = ["classic"];

export interface DeckTheme {
  id: string;
  name: Record<string, string>;
  builtin: boolean;
  error: string | null;
}

type Rec = Record<string, unknown>;
const rec = (v: unknown): Rec => (v && typeof v === "object" && !Array.isArray(v) ? (v as Rec) : {});
const THEME_ID = /^[a-z0-9_-]+$/;

/** A `/themes` body -> themes (malformed rows dropped), or null when it is not a list. */
export function parseThemes(raw: unknown): DeckTheme[] | null {
  if (!Array.isArray(raw)) return null;
  const out: DeckTheme[] = [];
  for (const item of raw) {
    const v = rec(item);
    if (typeof v.id !== "string" || !THEME_ID.test(v.id)) continue;
    const name = Object.fromEntries(
      Object.entries(rec(v.name)).filter(([, n]) => typeof n === "string"),
    ) as Record<string, string>;
    out.push({ id: v.id, name, builtin: v.builtin === true, error: typeof v.error === "string" ? v.error : null });
  }
  return out;
}

export function fallbackThemes(): DeckTheme[] {
  return BUILTIN_DECK_THEMES.map((id) => ({ id, name: {}, builtin: true, error: null }));
}

/** The runtime's themes; the built-ins when there is no runtime or the call fails. */
export async function fetchThemes(invoke: InvokeFn | null): Promise<DeckTheme[]> {
  if (!invoke) return fallbackThemes();
  try {
    const raw = rec(await invoke("maintenance_call", { method: "GET", path: "/themes" }));
    if (raw.status !== 200) return fallbackThemes();
    return parseThemes(raw.body) ?? fallbackThemes();
  } catch {
    return fallbackThemes();
  }
}

/** Select options: the built-ins first, then every user theme that loaded
 *  (a broken one is reported separately, never offered). */
export function themeOptions(themes: DeckTheme[]): string[] {
  const loaded = themes.filter((t) => t.error == null && !BUILTIN_DECK_THEMES.includes(t.id)).map((t) => t.id);
  return [...BUILTIN_DECK_THEMES, ...loaded];
}
