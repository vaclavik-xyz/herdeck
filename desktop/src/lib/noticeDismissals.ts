// "×" on a health notice hides it until that problem's CONTENT changes (a new
// outage, other versions, another error text): the dismissal is stored as
// {problem key → content} in localStorage, so it survives a restart of the
// app but never hides a different occurrence of the same kind of problem.
// Storage can be missing or throw (private mode, quota, jsdom): every access
// is wrapped and a failure just means "nothing dismissed".

export const DISMISSALS_KEY = "herdeck.noticeDismissals.v1";

export type Dismissals = Record<string, string>;

type StorageLike = Pick<Storage, "getItem" | "setItem">;

function storage(): StorageLike | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

export function readDismissals(store: StorageLike | null = storage()): Dismissals {
  if (!store) return {};
  try {
    const raw = store.getItem(DISMISSALS_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : {};
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return Object.fromEntries(
      Object.entries(parsed as Record<string, unknown>).filter(
        (e): e is [string, string] => typeof e[1] === "string",
      ),
    );
  } catch {
    return {};
  }
}

export function writeDismissals(d: Dismissals, store: StorageLike | null = storage()): void {
  if (!store) return;
  try {
    store.setItem(DISMISSALS_KEY, JSON.stringify(d));
  } catch {
    // Storage full / blocked: the dismissal just lasts for this session.
  }
}

export function isDismissed(d: Dismissals, n: { key: string; content: string }): boolean {
  return d[n.key] === n.content;
}

export function dismiss(d: Dismissals, n: { key: string; content: string }): Dismissals {
  return { ...d, [n.key]: n.content };
}

/** Drop dismissals of problems that are gone, so the SAME problem coming
 *  back later (e.g. the same version mismatch after a rollback) shows again.
 *  Only call it with a successfully read /health — an unreachable runtime
 *  says nothing about which problems are gone. */
export function pruneDismissals(d: Dismissals, presentKeys: Iterable<string>): Dismissals {
  const present = new Set(presentKeys);
  const kept = Object.entries(d).filter(([k]) => present.has(k));
  return kept.length === Object.keys(d).length ? d : Object.fromEntries(kept);
}
