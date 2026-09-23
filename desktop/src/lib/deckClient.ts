// Framework-free poll / diff / press core for the DeckView. This is a faithful
// port of the proven loop in the herdeck web simulator
// (src/herdeck/driver/web.py `_PAGE`): poll `GET /state`, gate on the monotonic
// `version`, refetch only the tiles whose per-tile version advanced (plus the
// panel when its version changes), and `POST /press/{i}` with the access token.
//
// Kept DOM- and Svelte-free so it is fully unit-testable under Vitest (mirroring
// sidecar.ts). DeckView.svelte is a thin template over these functions.

import type { Lang } from "./i18n.svelte";

/** Footer counts the sidecar reports in `/state.summary`. */
export interface DeckSummary {
  agents: number;
  blocked: number;
  working: number;
  idle: number;
  done: number;
  waiting: number; // panes held pending background work (herdwatch)
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
  labels: Record<number, string>; // tile index -> localized accessible description
  connections: Record<string, boolean>; // server/session id -> live connection state
  localConnections: Record<string, string>; // local session name -> collision-safe runtime id
  summary: DeckSummary;
  source: string; // "mock" | "live"
  connected: boolean;
  language: Lang;
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
  return { agents: 0, blocked: 0, working: 0, idle: 0, done: 0, waiting: 0 };
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
    waiting: num(v.waiting),
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

function parseConnections(raw: unknown): Record<string, boolean> {
  const out: Record<string, boolean> = {};
  if (raw == null || typeof raw !== "object" || Array.isArray(raw)) return out;
  for (const [id, connected] of Object.entries(raw as Record<string, unknown>)) {
    if (id && typeof connected === "boolean") out[id] = connected;
  }
  return out;
}

function parseLocalConnections(raw: unknown): Record<string, string> {
  const out: Record<string, string> = {};
  if (raw == null || typeof raw !== "object" || Array.isArray(raw)) return out;
  for (const [name, runtimeId] of Object.entries(raw as Record<string, unknown>)) {
    if (name && typeof runtimeId === "string" && runtimeId) out[name] = runtimeId;
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
    // Same shape as tile_sections (index -> non-empty string), so the same
    // normalizer applies. Older runtimes omit it -> {} -> "tile N" fallback.
    labels: parseSections(v.tile_labels),
    connections: parseConnections(v.connections),
    localConnections: parseLocalConnections(v.local_connections),
    summary: parseSummary(v.summary),
    source: typeof v.source === "string" ? v.source : "unknown",
    connected: v.connected === true,
    language: v.language === "cs" ? "cs" : "en",
  };
}

/** A compact one-line footer label, e.g. "4 agents · 2 working · ⚠ 1 blocked"
 *  ("4 agenti · 2 pracují · ⚠ 1 blokován" in cs). Blocked is emphasized last
 *  so it stands out. Framework-free: the caller passes the language. */
const csAgents = (n: number): string => (n === 1 ? "agent" : n >= 2 && n <= 4 ? "agenti" : "agentů");

export function summaryLabel(s: DeckSummary, lang: Lang = "en"): string {
  if (lang === "cs") {
    const parts: string[] = [`${s.agents} ${csAgents(s.agents)}`];
    if (s.working) parts.push(`${s.working} ${s.working === 1 ? "pracuje" : "pracují"}`);
    if (s.waiting) parts.push(`${s.waiting} v pozadí`);
    if (s.idle) parts.push(`${s.idle} ${s.idle === 1 ? "nečinný" : "nečinní"}`);
    if (s.done) parts.push(`${s.done} hotovo`);
    if (s.blocked) parts.push(`⚠ ${s.blocked} ${s.blocked === 1 ? "blokován" : "blokováni"}`);
    return parts.join(" · ");
  }
  const parts: string[] = [`${s.agents} ${s.agents === 1 ? "agent" : "agents"}`];
  if (s.working) parts.push(`${s.working} working`);
  if (s.waiting) parts.push(`${s.waiting} waiting`);
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

  /** The `/state.version` whose images are ALL loaded, or -1. This is the
   *  long-poll `after` cursor: a partially loaded step leaves it stale, so the
   *  next poll answers immediately and retries the failed image(s) instead of
   *  parking on a version the window never finished drawing. */
  get syncedVersion(): number {
    return this.syncedV;
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
 *  fetches are async because the production transport may proxy them through
 *  Tauri commands (the Rust shell injects the access token and dodges CORS);
 *  normally it returns token-free `herdeck://` scheme URLs the Rust shell
 *  serves, with base64 `data:` URLs as the fallback. */
export interface DeckTransport {
  /** Raw `GET /state` JSON (unparsed — stepDeck runs it through parseState).
   *  With `poll` it is a long-poll: the runtime holds the request until its
   *  version differs from `after` or `waitMs` elapses (contract C2). A runtime
   *  that predates C2 ignores both and answers at once; callers detect that. */
  fetchState(poll?: StatePoll): Promise<unknown>;
  /** `<img src>` for tile `index` at `version`, or null when absent (404). */
  tileImage(index: number, version: number): Promise<string | null>;
  /** `<img src>` for the panel at `version`, or null when absent. */
  panelImage(version: number): Promise<string | null>;
  /** `POST /press/{index}`. */
  press(index: number): Promise<PressResult>;
  /** Present when `tileImage`/`panelImage` hand out URLs the `<img>` loads by
   *  itself (the Tauri custom scheme). When such an image fails to load, the
   *  view calls this: it switches the transport to its self-contained fallback
   *  (base64 `data:` URLs) for good and returns the replacement src. */
  imageFallback?: {
    tile(index: number): Promise<string | null>;
    panel(): Promise<string | null>;
  };
}

/** Long-poll parameters for `GET /state` (contract C2). */
export interface StatePoll {
  after: number; // the version the client already has
  waitMs: number; // how long the runtime may hold the request (it clamps to 25s)
}

/** The runtime clamps `wait_ms` to 25000; stay under it so the Rust proxy's
 *  read timeout (wait + 5s) is never the thing that ends a quiet poll. */
export const LONG_POLL_MS = 20000;

/** The Tauri `invoke` shape, injected so deckClient stays framework-free (no
 *  `@tauri-apps/api` import) and the transport is unit-testable with a fake. */
export type InvokeFn = (cmd: string, args?: Record<string, unknown>) => Promise<unknown>;

/** Origin of the Rust shell's `herdeck` URI scheme, which serves tile/panel
 *  PNGs straight from the runtime (token injected Rust-side). WebView2 on
 *  Windows cannot load custom schemes directly and maps them to
 *  `http://<scheme>.localhost`; WKWebView/WebKitGTK use `<scheme>://localhost`. */
export function imageSchemeBase(userAgent: string): string {
  return /Windows/i.test(userAgent) ? "http://herdeck.localhost" : "herdeck://localhost";
}

export interface CommandTransportOptions {
  /** Serve images via the `herdeck` URI scheme (default) or, when false, only
   *  via the base64 `deck_tile`/`deck_panel` commands. */
  imageScheme?: boolean;
  /** Override for the scheme origin (tests); defaults from the user agent. */
  schemeBase?: string;
}

/** The production transport: every sidecar call goes through a token-free Tauri
 *  command (`deck_state` / `deck_press`) or the `herdeck` URI scheme. The Rust
 *  shell injects the access token and performs the request Rust-side, so the
 *  token never lives in JS and there is no cross-origin/CORS problem.
 *
 *  Images: a scheme URL (`herdeck://localhost/tile/3?v=17`) lets the WebView
 *  fetch and decode the PNG natively — no base64 round trip through IPC and a
 *  JS string per frame. The `?v=` makes every version a distinct, cacheable
 *  URL. If a scheme image fails to load (older shell without the scheme), the
 *  view calls `imageFallback`, which pins this transport to the base64
 *  `deck_tile`/`deck_panel` commands from then on. */
export function commandTransport(
  invoke: InvokeFn,
  options: CommandTransportOptions = {},
): DeckTransport {
  let scheme = options.imageScheme ?? true;
  const base =
    options.schemeBase ??
    imageSchemeBase(typeof navigator === "undefined" ? "" : navigator.userAgent);
  const tileData = async (index: number): Promise<string | null> => {
    const src = await invoke("deck_tile", { index });
    return typeof src === "string" && src ? src : null;
  };
  const panelData = async (): Promise<string | null> => {
    const src = await invoke("deck_panel");
    return typeof src === "string" && src ? src : null;
  };
  const transport: DeckTransport = {
    // No args for a plain poll: keeps the call identical to what a shell that
    // predates C2 expects. Tauri maps `waitMs` onto the Rust `wait_ms` arg.
    fetchState: (poll) =>
      poll ? invoke("deck_state", { after: poll.after, waitMs: poll.waitMs }) : invoke("deck_state"),
    async tileImage(index, version) {
      return scheme ? `${base}/tile/${index}?v=${version}` : tileData(index);
    },
    async panelImage(version) {
      return scheme ? `${base}/panel?v=${version}` : panelData();
    },
    async press(index) {
      const status = await invoke("deck_press", { index });
      const code = typeof status === "number" ? status : 0;
      return { ok: code >= 200 && code < 300, status: code, forbidden: code === 403 };
    },
  };
  if (scheme) {
    // Pin to base64 only when the command path HAS the image the scheme failed
    // to load — that proves the scheme is what is broken. A null answer is a
    // genuine 404 (the tile vanished between /state and the load), which must
    // not cost every later frame the fast path.
    const pinIfServed = (src: string | null): string | null => {
      if (src) scheme = false;
      return src;
    };
    transport.imageFallback = {
      tile: async (index) => pinIfServed(await tileData(index)),
      panel: async () => pinIfServed(await panelData()),
    };
  }
  return transport;
}

/** The plain-browser transport (the desktop UI served outside Tauri, e.g. the
 *  web simulator / vite preview): talks HTTP to the runtime directly with the
 *  token as a query param (header for presses), and hands out direct image
 *  URLs the `<img>` loads itself. Long-polls with `?after=&wait_ms=` (C2). */
export function httpTransport(
  baseUrl: string,
  token: string,
  fetchFn: typeof fetch = (...a) => fetch(...a),
): DeckTransport {
  const root = baseUrl.replace(/\/+$/, "");
  const q = (extra: Record<string, string | number> = {}): string => {
    const p = new URLSearchParams({ token });
    for (const [k, v] of Object.entries(extra)) p.set(k, String(v));
    return p.toString();
  };
  return {
    async fetchState(poll) {
      const extra: Record<string, number> = poll ? { after: poll.after, wait_ms: poll.waitMs } : {};
      const r = await fetchFn(`${root}/state?${q(extra)}`);
      if (!r.ok) throw new Error(`/state HTTP ${r.status}`);
      return r.json();
    },
    tileImage: async (index, version) => `${root}/tile/${index}?${q({ v: version })}`,
    panelImage: async (version) => `${root}/panel?${q({ v: version })}`,
    async press(index) {
      const r = await fetchFn(`${root}/press/${index}`, {
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
  language: Lang;
  tiles: Record<number, string>; // index -> img src
  sections: Record<number, string>; // index -> config section key (klik-to-jump)
  labels: Record<number, string>; // index -> localized accessible description
  connections: Record<string, boolean>; // server/session id -> live connection state
  localConnections: Record<string, string>; // local session name -> collision-safe runtime id
  panel: string | null;
}

export function initialView(slots = 13): DeckViewModel {
  return {
    online: false,
    slots,
    source: "unknown",
    connected: false,
    summary: emptySummary(),
    language: "en",
    tiles: {},
    sections: {},
    labels: {},
    connections: {},
    localConnections: {},
    panel: null,
  };
}

/** Debounces the offline verdict: one failed poll is noise (a runtime busy
 *  rendering, a port hand-over, a dropped long-poll), and flipping `online`
 *  for it flashed "Waiting for the runtime" over a working deck for a poll
 *  interval and made the aria-live footer announce "offline" to screen-reader
 *  users every time. Offline is reported only after `maxFailures` consecutive
 *  failures OR once the first of an unbroken failure run is `graceMs` old. */
export class OfflineDebounce {
  private failures = 0;
  private since = 0;

  constructor(
    private readonly maxFailures = 3,
    private readonly graceMs = 1000,
    private readonly now: () => number = () => Date.now(),
  ) {}

  /** Record a failed poll; true once the deck should be reported offline. */
  fail(): boolean {
    if (this.failures === 0) this.since = this.now();
    this.failures += 1;
    return this.failures >= this.maxFailures || this.now() - this.since >= this.graceMs;
  }

  /** Record a successful poll (ends the failure run). */
  ok(): void {
    this.failures = 0;
  }

  /** True while a failure run is in progress (the deck is being given grace). */
  get failing(): boolean {
    return this.failures > 0;
  }
}

/** Optional knobs for one stepDeck call. */
export interface StepOptions {
  /** Long-poll hold time; 0/absent = plain poll. Only used once the differ is
   *  fully synced (there is a version worth waiting on). */
  waitMs?: number;
  /** Offline debouncer; absent = any failure is reported offline at once. */
  offline?: OfflineDebounce;
}

/** A long-poll that comes back unchanged faster than this was not held by the
 *  runtime: it predates C2 and ignored `after`/`wait_ms`. */
export const LONG_POLL_IGNORED_MS = 1000;
/** Floor between two long-polls whose version changed, so a runtime bumping
 *  its version on every animation tick cannot spin the loop flat out. */
export const LONG_POLL_MIN_GAP_MS = 50;

/** How long to wait before the next `/state` step, given how this one went.
 *  - failing / not fully synced -> `pollMs` retry cadence (the old interval);
 *  - just synced by a plain poll -> 0: start long-polling right away;
 *  - long-poll came back unchanged almost at once -> the runtime ignored the
 *    params, so fall back to interval polling at `pollMs`;
 *  - otherwise (a change, or a quiet poll that timed out) -> re-arm at once
 *    (minus a small floor between changes). */
export function nextPollDelay(p: {
  pollMs: number;
  failing: boolean; // the step failed (inside or past the offline grace)
  longPolled: boolean; // the step asked the runtime to hold the request
  before: number; // differ.syncedVersion before the step
  after: number; // differ.syncedVersion after the step
  elapsedMs: number; // how long the step took
}): number {
  if (p.failing || p.after < 0) return p.pollMs;
  if (!p.longPolled) return 0;
  if (p.after === p.before && p.elapsedMs < LONG_POLL_IGNORED_MS) return p.pollMs;
  return Math.max(0, LONG_POLL_MIN_GAP_MS - p.elapsedMs);
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
  options: StepOptions = {},
): Promise<DeckViewModel> {
  const { waitMs = 0, offline } = options;
  const failed = (): DeckViewModel => {
    // Inside the grace window keep the last good model (identity: no re-render,
    // no aria-live announcement); only a sustained failure flips `online`.
    if (offline && !offline.fail()) return prev;
    return { ...prev, online: false };
  };
  const after = differ.syncedVersion;
  let raw: unknown;
  try {
    raw = await transport.fetchState(waitMs > 0 && after >= 0 ? { after, waitMs } : undefined);
  } catch {
    return failed();
  }
  const state = parseState(raw);
  if (!state) return failed();
  offline?.ok();

  const diff = differ.plan(state);
  // Nothing to fetch and nothing visible changed: return prev UNCHANGED so the
  // $state assignment is a no-op — the idle 300ms poll used to mint a fresh
  // view model (new tiles map) that re-derived and re-reconciled the whole
  // template 3.3x per second for zero visual change.
  if (
    diff.refetch.length === 0 &&
    diff.clear.length === 0 &&
    !diff.panel &&
    prev.online &&
    prev.source === state.source &&
    prev.connected === state.connected &&
    prev.language === state.language &&
    (state.slots === 0 || prev.slots === state.slots) &&
    sameSummary(prev.summary, state.summary) &&
    sameRecords(prev.sections, state.sections) &&
    sameRecords(prev.labels, state.labels) &&
    sameRecords(prev.connections, state.connections) &&
    sameRecords(prev.localConnections, state.localConnections)
  ) {
    differ.markSynced(state.version);
    return prev;
  }
  const tiles = { ...prev.tiles };
  let allLoaded = true;
  // Refetch the changed tiles AND the panel concurrently (the panel used to
  // wait for the tiles' Promise.all, landing one round trip late). Commit a
  // version only once its image resolves; on a fetch error keep the old src
  // and leave the version uncommitted so the next poll retries it.
  let panel = prev.panel;
  await Promise.all([
    ...diff.refetch.map(async ({ index, version }) => {
      try {
        const src = await transport.tileImage(index, version);
        if (src) tiles[index] = src;
        else delete tiles[index];
        differ.commitTile(index, version);
      } catch {
        allLoaded = false; // leave previous src; retried next poll
      }
    }),
    (async () => {
      if (!diff.panel) return;
      try {
        panel = await transport.panelImage(diff.panel.version);
        differ.commitPanel(diff.panel.version);
      } catch {
        allLoaded = false; // keep previous panel; retried next poll
      }
    })(),
  ]);
  for (const index of diff.clear) {
    delete tiles[index];
    differ.dropTile(index);
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
    language: state.language,
    tiles,
    sections: state.sections,
    labels: state.labels,
    connections: state.connections,
    localConnections: state.localConnections,
    panel,
  };
}

function sameSummary(a: DeckSummary, b: DeckSummary): boolean {
  return (
    a.agents === b.agents &&
    a.blocked === b.blocked &&
    a.working === b.working &&
    a.idle === b.idle &&
    a.done === b.done &&
    a.waiting === b.waiting
  );
}

function sameRecords<T>(a: Record<string | number, T>, b: Record<string | number, T>): boolean {
  const ak = Object.keys(a);
  const bk = Object.keys(b);
  if (ak.length !== bk.length) return false;
  for (const k of ak) if (a[k] !== b[k]) return false;
  return true;
}
