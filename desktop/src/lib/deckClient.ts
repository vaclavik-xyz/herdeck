// Framework-free poll / diff / press core for the DeckView. This is a faithful
// port of the proven loop in the herdeck web simulator
// (src/herdeck/driver/web.py `_PAGE`): poll `GET /state`, gate on the monotonic
// `version`, refetch only the tiles whose per-tile version advanced (plus the
// panel when its version changes), and `POST /press/{i}` with the access token.
//
// Kept DOM- and Svelte-free so it is fully unit-testable under Vitest (mirroring
// sidecar.ts). DeckView.svelte is a thin template over these functions.

/** Footer counts the sidecar reports in `/state.summary`. */
export interface DeckSummary {
  agents: number;
  blocked: number;
  working: number;
  idle: number;
  done: number;
}

/** The parsed `/state` snapshot. `tiles`/`panel` carry per-element *versions*
 *  (not pixels): the client refetches PNGs only when a version advances. */
export interface DeckState {
  version: number;
  slots: number;
  hasPanel: boolean;
  panel: number; // panel image version
  tiles: Record<number, number>; // tile index -> image version
  summary: DeckSummary;
  source: string; // "mock" | "live"
  connected: boolean;
}

/** The actions a single `/state` advance implies, as computed by DeckDiffer. */
export interface DeckDiff {
  /** Tiles whose version advanced (or are new): refetch `GET /tile/{index}`. */
  refetch: { index: number; version: number }[];
  /** Tiles that disappeared from `/state`: clear the cell. */
  clear: number[];
  /** The panel advanced: refetch `GET /panel`. Null when unchanged. */
  panel: { version: number } | null;
}

export function emptySummary(): DeckSummary {
  return { agents: 0, blocked: 0, working: 0, idle: 0, done: 0 };
}

function num(v: unknown, fallback = 0): number {
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}

function parseSummary(raw: unknown): DeckSummary {
  const v = (raw ?? {}) as Record<string, unknown>;
  return {
    agents: num(v.agents),
    blocked: num(v.blocked),
    working: num(v.working),
    idle: num(v.idle),
    done: num(v.done),
  };
}

/** Normalize the JSON `tiles` object (string keys) into a numeric-keyed map,
 *  dropping any non-integer index or non-numeric version. */
function parseTiles(raw: unknown): Record<number, number> {
  const out: Record<number, number> = {};
  if (raw == null || typeof raw !== "object") return out;
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    const i = Number(k);
    if (Number.isInteger(i) && i >= 0 && typeof v === "number" && Number.isFinite(v)) {
      out[i] = v;
    }
  }
  return out;
}

/** Shape a raw `/state` JSON value into a DeckState, or null when it is not a
 *  usable snapshot (so the caller can treat it as an offline tick). */
export function parseState(raw: unknown): DeckState | null {
  if (raw == null || typeof raw !== "object") return null;
  const v = raw as Record<string, unknown>;
  if (typeof v.version !== "number") return null;
  return {
    version: v.version,
    slots: num(v.slots),
    hasPanel: v.has_panel === true,
    panel: num(v.panel, -1),
    tiles: parseTiles(v.tiles),
    summary: parseSummary(v.summary),
    source: typeof v.source === "string" ? v.source : "unknown",
    connected: v.connected === true,
  };
}

/** A compact one-line footer label, e.g. "4 agents · 2 working · 1 idle · ⚠ 1
 *  blocked". Blocked is emphasized last so it stands out. */
export function summaryLabel(s: DeckSummary): string {
  const parts: string[] = [`${s.agents} ${s.agents === 1 ? "agent" : "agents"}`];
  if (s.working) parts.push(`${s.working} working`);
  if (s.idle) parts.push(`${s.idle} idle`);
  if (s.done) parts.push(`${s.done} done`);
  if (s.blocked) parts.push(`⚠ ${s.blocked} blocked`);
  return parts.join(" · ");
}

/** Stateful version gate + per-tile diff, ported from web.py's poll(): holds the
 *  last seen overall version, per-tile versions and panel version, and on each
 *  snapshot returns only the work that actually changed. */
export class DeckDiffer {
  private lastV = -1;
  private tv: Record<number, number> = {};
  private pv = -1;

  /** Reset tracking so the next reconcile refetches everything (e.g. after a
   *  reconnect, where the sidecar may have restarted its version counter). */
  reset(): void {
    this.lastV = -1;
    this.tv = {};
    this.pv = -1;
  }

  reconcile(state: DeckState): DeckDiff {
    const diff: DeckDiff = { refetch: [], clear: [], panel: null };
    if (state.version === this.lastV) return diff; // cheap gate: nothing changed
    this.lastV = state.version;

    const next = state.tiles;
    const indices = new Set<number>();
    for (const k of Object.keys(this.tv)) indices.add(Number(k));
    for (const k of Object.keys(next)) indices.add(Number(k));
    for (const i of indices) {
      const v = next[i];
      if (v === undefined) {
        if (this.tv[i] !== undefined) {
          delete this.tv[i];
          diff.clear.push(i);
        }
      } else if (v !== this.tv[i]) {
        this.tv[i] = v;
        diff.refetch.push({ index: i, version: v });
      }
    }
    if (state.hasPanel && state.panel !== this.pv) {
      this.pv = state.panel;
      diff.panel = { version: this.pv };
    }
    return diff;
  }
}

/** Result of a press POST. `ok` mirrors web.py's `r.ok`; `forbidden` flags a 403
 *  (stale/invalid token) so the shell can re-pull discovery. */
export interface PressResult {
  ok: boolean;
  status: number;
  forbidden: boolean;
}

/** How DeckView talks to the sidecar. Injectable so the view (and stepDeck) are
 *  testable with a fake, and so the real transport (direct loopback fetch with
 *  the access token) lives in one place. */
export interface DeckTransport {
  /** Raw `GET /state` JSON (unparsed — stepDeck runs it through parseState). */
  fetchState(): Promise<unknown>;
  /** `<img src>` for tile `index` at `version` (token-authed, cache-busted). */
  tileSrc(index: number, version: number): string;
  /** `<img src>` for the panel at `version`. */
  panelSrc(version: number): string;
  /** `POST /press/{index}`. */
  press(index: number): Promise<PressResult>;
}

/** Append the access token to a GET url as a query param (web.py `auth()`). */
function withToken(url: string, token: string): string {
  return url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(token);
}

/** Direct loopback fetch to the sidecar, authed with the one-time access token
 *  exactly as web.py does — `?token=` on GETs, `X-Herdeck-Token` header on the
 *  press POST. `base` is the sidecar origin (e.g. "http://127.0.0.1:51234"),
 *  with no trailing slash.
 *
 *  CROSS-SLICE NOTE: web.py serves its page FROM the sidecar, so its fetch is
 *  same-origin. In the Tauri WebView the page origin differs from the loopback
 *  sidecar, so this direct path is cross-origin and the sidecar (slice 1) sends
 *  no CORS headers — it will be blocked until either the sidecar adds CORS for
 *  the WebView origin (src/herdeck/deckapp) OR this is swapped for a transport
 *  that proxies `/state`,`/tile`,`/panel`,`/press` through Tauri commands
 *  (desktop/src-tauri, the shell's preferred design). Both are outside slice 2's
 *  owned paths; until one lands the deck renders its offline state. The
 *  DeckTransport seam means that follow-up is a drop-in, with no DeckView change. */
export function directFetchTransport(base: string, token: string): DeckTransport {
  const origin = base.replace(/\/$/, "");
  return {
    async fetchState() {
      const r = await fetch(withToken(origin + "/state", token));
      if (!r.ok) throw new Error(`state ${r.status}`);
      return r.json();
    },
    tileSrc(index, version) {
      return withToken(`${origin}/tile/${index}?v=${version}`, token);
    },
    panelSrc(version) {
      return withToken(`${origin}/panel?v=${version}`, token);
    },
    async press(index) {
      const r = await fetch(`${origin}/press/${index}`, {
        method: "POST",
        headers: { "X-Herdeck-Token": token },
      });
      return { ok: r.ok, status: r.status, forbidden: r.status === 403 };
    },
  };
}

/** The render model DeckView binds to. `tiles`/`panel` hold ready-to-use `<img
 *  src>` strings; `online` is false when the last poll failed (offline UI). */
export interface DeckViewModel {
  online: boolean;
  slots: number;
  source: string;
  connected: boolean;
  summary: DeckSummary;
  tiles: Record<number, string>; // index -> img src
  panel: string | null;
}

export function initialView(slots = 13): DeckViewModel {
  return {
    online: false,
    slots,
    source: "unknown",
    connected: false,
    summary: emptySummary(),
    tiles: {},
    panel: null,
  };
}

/** One poll step: fetch + parse `/state`, run the diff, and fold the changed
 *  tile/panel srcs into a fresh view model. A fetch/parse failure yields an
 *  offline model that keeps the last-known tiles (so the grid doesn't flash).
 *  Pure given its inputs (the differ carries the version tracking), so the whole
 *  poll behavior is unit-testable without a DOM or timers. */
export async function stepDeck(
  transport: DeckTransport,
  differ: DeckDiffer,
  prev: DeckViewModel,
): Promise<DeckViewModel> {
  let raw: unknown;
  try {
    raw = await transport.fetchState();
  } catch {
    return { ...prev, online: false };
  }
  const state = parseState(raw);
  if (!state) return { ...prev, online: false };

  const diff = differ.reconcile(state);
  const tiles = { ...prev.tiles };
  for (const { index, version } of diff.refetch) {
    tiles[index] = transport.tileSrc(index, version);
  }
  for (const index of diff.clear) delete tiles[index];
  let panel = prev.panel;
  if (diff.panel) panel = transport.panelSrc(diff.panel.version);

  return {
    online: true,
    slots: state.slots || prev.slots,
    source: state.source,
    connected: state.connected,
    summary: state.summary,
    tiles,
    panel,
  };
}
