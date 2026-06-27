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
  sections: Record<number, string>; // tile index -> config section key (klik-to-jump)
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

/** Normalize the JSON `tile_sections` object (string keys, string values) into a
 *  numeric-keyed map, dropping non-integer indices or non-string section values. */
function parseSections(raw: unknown): Record<number, string> {
  const out: Record<number, string> = {};
  if (raw == null || typeof raw !== "object") return out;
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    const i = Number(k);
    if (Number.isInteger(i) && i >= 0 && typeof v === "string" && v) out[i] = v;
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
    sections: parseSections(v.tile_sections),
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

/** Stateful version gate + per-tile diff, ported from web.py's poll() but made
 *  *transactional* for the async (proxy) transport: `plan()` decides what needs
 *  fetching WITHOUT committing, and the caller commits each tile/panel version
 *  only once its image has actually loaded. A version is marked "synced" (so the
 *  cheap gate can skip it next time) only when the whole step succeeded — so a
 *  transient image fetch failure is retried on the next poll instead of being
 *  silently lost (web.py got this for free: its `<img>` retried the GET itself). */
export class DeckDiffer {
  private syncedV = -1; // state.version whose images are all loaded
  private tv: Record<number, number> = {}; // committed (loaded) tile versions
  private pv = -1; // committed (loaded) panel version

  /** Reset tracking so the next plan refetches everything (e.g. after a
   *  reconnect, where the sidecar may have restarted its version counter). */
  reset(): void {
    this.syncedV = -1;
    this.tv = {};
    this.pv = -1;
  }

  /** What still needs fetching for `state`, WITHOUT committing anything. Returns
   *  an empty diff when the version is already fully synced (the cheap gate). */
  plan(state: DeckState): DeckDiff {
    const diff: DeckDiff = { refetch: [], clear: [], panel: null };
    if (state.version === this.syncedV) return diff; // nothing changed at all

    const next = state.tiles;
    const indices = new Set<number>();
    for (const k of Object.keys(this.tv)) indices.add(Number(k));
    for (const k of Object.keys(next)) indices.add(Number(k));
    for (const i of indices) {
      const v = next[i];
      if (v === undefined) {
        if (this.tv[i] !== undefined) diff.clear.push(i);
      } else if (v !== this.tv[i]) {
        diff.refetch.push({ index: i, version: v });
      }
    }
    if (state.hasPanel && state.panel !== this.pv) {
      diff.panel = { version: state.panel };
    }
    return diff;
  }

  /** Record that tile `index`'s image for `version` is now loaded. */
  commitTile(index: number, version: number): void {
    this.tv[index] = version;
  }

  /** Forget a tile that disappeared from `/state`. */
  dropTile(index: number): void {
    delete this.tv[index];
  }

  /** Record that the panel image for `version` is now loaded. */
  commitPanel(version: number): void {
    this.pv = version;
  }

  /** Arm the cheap gate for `version`. Call only once every changed image in the
   *  step has loaded, so a partial step is re-planned (and retried) next poll. */
  markSynced(version: number): void {
    this.syncedV = version;
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
 *  testable with a fake, and so the real transport lives in one place. Image
 *  fetches are async because the production transport proxies them through Tauri
 *  commands (the Rust shell injects the access token and dodges CORS), returning
 *  ready-to-use `data:` URLs rather than direct (cross-origin, token-bearing)
 *  sidecar URLs. */
export interface DeckTransport {
  /** Raw `GET /state` JSON (unparsed — stepDeck runs it through parseState). */
  fetchState(): Promise<unknown>;
  /** `<img src>` for tile `index` at `version`, or null when absent (404). */
  tileImage(index: number, version: number): Promise<string | null>;
  /** `<img src>` for the panel at `version`, or null when absent. */
  panelImage(version: number): Promise<string | null>;
  /** `POST /press/{index}`. */
  press(index: number): Promise<PressResult>;
}

/** The Tauri `invoke` shape, injected so deckClient stays framework-free (no
 *  `@tauri-apps/api` import) and the transport is unit-testable with a fake. */
export type InvokeFn = (cmd: string, args?: Record<string, unknown>) => Promise<unknown>;

/** The production transport: every sidecar call goes through a token-free Tauri
 *  command (`deck_state` / `deck_tile` / `deck_panel` / `deck_press`). The Rust
 *  shell injects the access token and performs the request Rust-side, so the
 *  token never lives in JS and there is no cross-origin/CORS problem. Tiles and
 *  the panel come back as `data:image/png;base64,…` URLs the `<img>` renders. */
export function commandTransport(invoke: InvokeFn): DeckTransport {
  return {
    fetchState: () => invoke("deck_state"),
    async tileImage(index) {
      const src = await invoke("deck_tile", { index });
      return typeof src === "string" && src ? src : null;
    },
    async panelImage() {
      const src = await invoke("deck_panel");
      return typeof src === "string" && src ? src : null;
    },
    async press(index) {
      const status = await invoke("deck_press", { index });
      const code = typeof status === "number" ? status : 0;
      return { ok: code >= 200 && code < 300, status: code, forbidden: code === 403 };
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
  sections: Record<number, string>; // index -> config section key (klik-to-jump)
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
    sections: {},
    panel: null,
  };
}

/** One poll step: fetch + parse `/state`, run the diff, and fold the changed
 *  tile/panel images into a fresh view model. A fetch/parse failure yields an
 *  offline model that keeps the last-known tiles (so the grid doesn't flash);
 *  a per-tile image failure keeps that tile's previous src. Pure given its
 *  inputs (the differ carries the version tracking), so the whole poll behavior
 *  is unit-testable without a DOM or timers. */
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

  const diff = differ.plan(state);
  const tiles = { ...prev.tiles };
  let allLoaded = true;
  // Refetch the changed tiles concurrently. Commit a tile's version only once its
  // image resolves (a data URL, or a definitive "none"); on a fetch error keep
  // the old src AND leave the version uncommitted so the next poll retries it.
  await Promise.all(
    diff.refetch.map(async ({ index, version }) => {
      try {
        const src = await transport.tileImage(index, version);
        if (src) tiles[index] = src;
        else delete tiles[index];
        differ.commitTile(index, version);
      } catch {
        allLoaded = false; // leave previous src; retried next poll
      }
    }),
  );
  for (const index of diff.clear) {
    delete tiles[index];
    differ.dropTile(index);
  }
  let panel = prev.panel;
  if (diff.panel) {
    try {
      panel = await transport.panelImage(diff.panel.version);
      differ.commitPanel(diff.panel.version);
    } catch {
      allLoaded = false; // keep previous panel; retried next poll
    }
  }
  // Arm the cheap gate only when the whole step loaded; otherwise the next poll
  // (same /state.version) re-plans and retries just the failed image(s).
  if (allLoaded) differ.markSynced(state.version);

  return {
    online: true,
    slots: state.slots || prev.slots,
    source: state.source,
    connected: state.connected,
    summary: state.summary,
    tiles,
    sections: state.sections,
    panel,
  };
}
