// Framework-free client for the desktop Maintenance section: the runtime's
// /maintenance* routes (src/herdeck/deckapp/maintenance.py + bridge_update.py)
// relayed by the Rust `maintenance_call` proxy (desktop/src-tauri/src/
// maintenance.rs, which injects the token and adds the shell's own `app`
// facts to GET /maintenance), plus the shell's `runtime_service` and
// `open_log` commands. Pure parsing + outcome mapping, unit-testable like
// agentCardClient.ts; the en/cs texts live in the components.

import type { InvokeFn } from "./deckClient";

export type D200State = "connected" | "not_on_usb" | "locked" | "disconnected" | "unsupervised" | "unknown";

export type PowerCycleReason = "uhubctl_missing" | "uhubctl_not_executable" | "location_unknown";

export interface PowerCycleStatus {
  available: boolean;
  reason: string | null;
  uhubctl: string | null;
  hub: string | null;
  port: number | null;
  source: string | null; // "config" | "last_seen"
}

export interface D200Status {
  state: D200State;
  connected: boolean;
  since: number | null; // ms epoch
  lastFrameAt: number | null; // ms epoch
  lastError: string | null;
  lockOwner: number | null;
  usbPresent: boolean | null;
  usbLocation: string | null;
  supervised: boolean;
  powerCycle: PowerCycleStatus;
}

export interface ServerStatus {
  id: string;
  /** true = its install may update itself, false = not a managed install,
   *  null = unknown (an older bridge, T3, or not asked yet). */
  managed: boolean | null;
  /** The bridge understands the `update` message. */
  selfUpdate: boolean;
  connected: boolean | null;
  bridgeVersion: string | null;
  protocolSupported: boolean | null;
  lastError: string | null;
  everConnected: boolean | null;
}

export interface AppFacts {
  version: string;
  channel: string;
  bundle: string | null;
  bundledRuntime: string | null;
  spawnedRuntime: boolean;
}

export interface MaintenanceStatus {
  version: string | null;
  pid: number | null;
  uptimeS: number | null;
  process: { frozen: boolean; executable: string | null; spawnedByApp: boolean; isService: boolean };
  service: { installed: boolean; label: string | null; unitPath: string | null; program: string | null; fromApp: boolean };
  logs: { runtime: string | null; app: string | null };
  d200: D200Status;
  servers: ServerStatus[];
  /** Why the existing config does not load (the runtime shows an error state), or null. */
  configError: string | null;
  app: AppFacts | null;
}

type Rec = Record<string, unknown>;
const rec = (v: unknown): Rec => (v && typeof v === "object" && !Array.isArray(v) ? (v as Rec) : {});
const str = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const bool = (v: unknown): boolean | null => (typeof v === "boolean" ? v : null);

const D200_STATES: readonly D200State[] = ["connected", "not_on_usb", "locked", "disconnected", "unsupervised"];

/** Shape a raw GET /maintenance body (null when it is not one). */
export function parseMaintenance(raw: unknown): MaintenanceStatus | null {
  if (raw == null || typeof raw !== "object" || Array.isArray(raw)) return null;
  const v = raw as Rec;
  const proc = rec(v.process);
  const svc = rec(v.service);
  const logs = rec(v.logs);
  const d = rec(v.d200);
  const pc = rec(d.power_cycle);
  const app = v.app && typeof v.app === "object" ? rec(v.app) : null;
  const state = D200_STATES.includes(d.state as D200State) ? (d.state as D200State) : "unknown";
  return {
    version: str(v.version),
    pid: num(v.pid),
    uptimeS: num(v.uptime_s),
    process: {
      frozen: proc.frozen === true,
      executable: str(proc.executable),
      spawnedByApp: proc.spawned_by_app === true,
      isService: proc.is_service === true,
    },
    service: {
      installed: svc.installed === true,
      label: str(svc.label),
      unitPath: str(svc.unit_path),
      program: str(svc.program),
      fromApp: svc.from_app === true,
    },
    logs: { runtime: str(logs.runtime), app: str(logs.app) },
    d200: {
      state,
      connected: d.connected === true,
      since: num(d.since),
      lastFrameAt: num(d.last_frame_at),
      lastError: str(d.last_error),
      lockOwner: num(d.lock_owner),
      usbPresent: bool(d.usb_present),
      usbLocation: str(d.usb_location),
      supervised: d.supervised === true,
      powerCycle: {
        available: pc.available === true,
        reason: str(pc.reason),
        uhubctl: str(pc.uhubctl),
        hub: str(pc.hub),
        port: num(pc.port),
        source: str(pc.source),
      },
    },
    configError: str(v.config_error),
    servers: Object.entries(rec(v.servers)).map(([id, value]) => {
      const s = rec(value);
      return {
        id,
        managed: bool(s.managed),
        selfUpdate: s.self_update === true,
        connected: bool(s.connected),
        bridgeVersion: str(s.bridge_version),
        protocolSupported: bool(s.protocol_supported),
        lastError: str(s.last_error),
        everConnected: bool(s.ever_connected),
      };
    }),
    app: app
      ? {
          version: str(app.version) ?? "",
          channel: str(app.channel) ?? "",
          bundle: str(app.bundle),
          bundledRuntime: str(app.bundled_runtime),
          spawnedRuntime: app.spawned_runtime === true,
        }
      : null,
  };
}

/** Where the runtime this window talks to comes from. */
export type RuntimeOrigin =
  | "service_this_app" // herdeck-service unit running THIS app's bundled runtime
  | "service_other_app" // a unit running another app bundle's runtime
  | "service_checkout" // a unit running a source checkout / venv
  | "self_spawned" // this app's own child process
  | "attached"; // something else started it (a hand-written unit, a terminal)

/** Who owns the INSTALLED runtime unit (whether or not it is the process
 *  this window talks to) — mirrors the Rust `runtime_service::unit_owner`
 *  guard: the app restarts only its own unit and replaces/removes another
 *  one only after a confirmation that names it. */
export type UnitOwner = "none" | "this_app" | "other_app" | "checkout";

export function unitOwner(s: MaintenanceStatus): UnitOwner {
  if (!s.service.installed) return "none";
  const program = s.service.program ?? "";
  const app = s.app;
  const ours = app != null && (
    (app.bundledRuntime != null && program === app.bundledRuntime)
    || (app.bundle != null && program.startsWith(`${app.bundle}/`))
  );
  if (s.service.fromApp) return ours ? "this_app" : "other_app";
  return "checkout";
}

export function runtimeOrigin(s: MaintenanceStatus): RuntimeOrigin {
  if (s.process.isService && s.service.installed) {
    const owner = unitOwner(s);
    return owner === "this_app" ? "service_this_app" : owner === "other_app" ? "service_other_app" : "service_checkout";
  }
  if (s.app?.spawnedRuntime) return "self_spawned";
  return "attached";
}

/** One row of the version overview; `mismatch` = differs from the runtime's. */
export interface VersionRow {
  kind: "app" | "runtime" | "bridge";
  id: string;
  version: string | null;
  mismatch: boolean;
}

export function versionRows(s: MaintenanceStatus): VersionRow[] {
  const runtime = s.version;
  const differs = (v: string | null): boolean => runtime != null && v != null && v !== runtime;
  const rows: VersionRow[] = [];
  if (s.app) rows.push({ kind: "app", id: "app", version: s.app.version || null, mismatch: differs(s.app.version || null) });
  rows.push({ kind: "runtime", id: "runtime", version: runtime, mismatch: false });
  for (const server of s.servers) {
    rows.push({ kind: "bridge", id: server.id, version: server.bridgeVersion, mismatch: differs(server.bridgeVersion) });
  }
  return rows;
}

/** Compare dotted release versions ("0.9.1", "v1.2.0rc1" → its numbers);
 *  null when either has no number at all. */
export function compareVersions(a: string, b: string): number | null {
  const parts = (v: string): number[] | null => {
    const m = v.match(/\d+(?:\.\d+)*/);
    return m ? m[0].split(".").map(Number) : null;
  };
  const x = parts(a);
  const y = parts(b);
  if (!x || !y) return null;
  for (let i = 0; i < Math.max(x.length, y.length); i += 1) {
    const d = (x[i] ?? 0) - (y[i] ?? 0);
    if (d !== 0) return Math.sign(d);
  }
  return 0;
}

/** What the Maintenance section offers for one bridge:
 *  - `update`: a managed, self-updating bridge older than the runtime;
 *  - `install_managed`: not a managed install — the one-time command;
 *  - `unknown`: install type unknown (older bridge / T3) — neutral text;
 *  - `unsupported`: managed but predates self-update — update it by hand;
 *  - `none`: up to date (or newer). */
export type BridgeOffer = "update" | "install_managed" | "unknown" | "unsupported" | "none";

export function bridgeOffer(server: ServerStatus, runtimeVersion: string | null): BridgeOffer {
  if (server.managed === false) return "install_managed";
  if (server.managed === null) return "unknown";
  if (!server.selfUpdate) return "unsupported";
  if (!server.bridgeVersion || !runtimeVersion) return "none";
  return compareVersions(server.bridgeVersion, runtimeVersion) === -1 ? "update" : "none";
}

// --- calls -------------------------------------------------------------------

/** `{status, body}` as the Rust proxy relays it. */
interface Relayed {
  status: number;
  body: unknown;
}

function relayed(raw: unknown): Relayed {
  const v = rec(raw);
  return { status: typeof v.status === "number" ? v.status : 0, body: v.body ?? null };
}

async function call(invoke: InvokeFn, method: "GET" | "POST", path: string, body?: Rec): Promise<Relayed> {
  return relayed(await invoke("maintenance_call", { method, path, ...(body ? { body } : {}) }));
}

/** encodeURIComponent plus the five characters it leaves alone, so the id is
 *  a single path segment the Rust allow-list accepts. */
export function serverSegment(id: string): string {
  return encodeURIComponent(id).replace(/[!'()*]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`);
}

export type StatusResult = { kind: "ok"; status: MaintenanceStatus } | { kind: "error"; message: string };

export async function fetchMaintenance(invoke: InvokeFn): Promise<StatusResult> {
  try {
    const r = await call(invoke, "GET", "/maintenance");
    if (r.status !== 200) return { kind: "error", message: `HTTP ${r.status}` };
    const status = parseMaintenance(r.body);
    return status ? { kind: "ok", status } : { kind: "error", message: "invalid /maintenance reply" };
  } catch (e) {
    return { kind: "error", message: String(e) };
  }
}

/** A deck action's result: the runtime's `outcome` plus its details; the
 *  transport-side codes are `http` and `unreachable`. */
export interface DeckOutcome {
  ok: boolean;
  outcome: string;
  error: string | null;
  command: string | null;
  reason: string | null;
  pid: number | null;
  usbPresent: boolean | null;
  hub: string | null;
  port: number | null;
}

export function parseDeckOutcome(r: Relayed): DeckOutcome {
  const b = rec(r.body);
  const base = { error: str(b.error), command: str(b.command), reason: str(b.reason), pid: num(b.pid), usbPresent: bool(b.usb_present), hub: str(b.hub), port: num(b.port) };
  if (r.status !== 200 || typeof b.outcome !== "string") {
    return { ...base, ok: false, outcome: "http", error: base.error ?? `HTTP ${r.status}` };
  }
  return { ...base, ok: b.ok === true, outcome: b.outcome };
}

async function deckAction(invoke: InvokeFn, path: string): Promise<DeckOutcome> {
  try {
    return parseDeckOutcome(await call(invoke, "POST", path, {}));
  } catch (e) {
    return { ok: false, outcome: "unreachable", error: String(e), command: null, reason: null, pid: null, usbPresent: null, hub: null, port: null };
  }
}

export const restartDeck = (invoke: InvokeFn): Promise<DeckOutcome> => deckAction(invoke, "/maintenance/deck/restart");
export const powerCycleDeck = (invoke: InvokeFn): Promise<DeckOutcome> => deckAction(invoke, "/maintenance/deck/power-cycle");

// --- bridge update -------------------------------------------------------------

export interface UpdateProgress {
  seq: number;
  stage: string;
  message: string;
}

export interface BridgeUpdateView {
  ok: boolean;
  /** updated · pending · current · newer (ok) · not_managed · readonly · failed ·
   *  busy · downgrade · unsupported ·
   *  disconnected · newer · current (runtime), http · unreachable (transport). */
  code: string;
  message: string;
  target: string | null;
  output: string;
  progress: UpdateProgress[]; // everything seen so far, in order
  next: number;
}

/** How long each update request asks the runtime to hold (it caps at 25 s). */
export const UPDATE_WAIT_MS = 20_000;

function parseUpdate(r: Relayed, prior: UpdateProgress[]): BridgeUpdateView {
  const b = rec(r.body);
  if (r.status !== 200 || typeof b.code !== "string") {
    return { ok: false, code: "http", message: `HTTP ${r.status}`, target: null, output: "", progress: prior, next: 0 };
  }
  const fresh: UpdateProgress[] = Array.isArray(b.progress)
    ? b.progress.flatMap((p) => {
        const e = rec(p);
        const seq = num(e.seq);
        return seq == null ? [] : [{ seq, stage: str(e.stage) ?? "", message: str(e.message) ?? "" }];
      })
    : [];
  const seen = new Set(prior.map((p) => p.seq));
  return {
    ok: b.ok === true,
    code: b.code,
    message: str(b.message) ?? "",
    target: str(b.target),
    output: str(b.output) ?? "",
    progress: [...prior, ...fresh.filter((p) => !seen.has(p.seq))],
    next: num(b.next) ?? 0,
  };
}

/** Ask a bridge to update itself and follow it to an outcome: POST, then
 *  long-poll GET ?after=<next> while the answer is `pending`. `onUpdate` sees
 *  every intermediate view; `isCancelled` stops the loop (unmounted UI). */
export async function runBridgeUpdate(
  invoke: InvokeFn,
  serverId: string,
  onUpdate: (view: BridgeUpdateView) => void = () => {},
  opts: { waitMs?: number; isCancelled?: () => boolean; maxPolls?: number } = {},
): Promise<BridgeUpdateView> {
  const waitMs = opts.waitMs ?? UPDATE_WAIT_MS;
  const path = `/maintenance/servers/${serverSegment(serverId)}/update`;
  let view: BridgeUpdateView;
  try {
    view = parseUpdate(await call(invoke, "POST", path, { wait_ms: waitMs }), []);
  } catch (e) {
    view = { ok: false, code: "unreachable", message: String(e), target: null, output: "", progress: [], next: 0 };
  }
  onUpdate(view);
  let polls = 0;
  const maxPolls = opts.maxPolls ?? 60; // 60 × 20 s ≥ the runtime's 900 s job deadline
  while (view.code === "pending" && !opts.isCancelled?.() && polls < maxPolls) {
    polls += 1;
    try {
      const r = await call(invoke, "GET", `${path}?after=${view.next}&wait_ms=${waitMs}`);
      view = parseUpdate(r, view.progress);
    } catch (e) {
      view = { ...view, ok: false, code: "unreachable", message: String(e) };
    }
    onUpdate(view);
  }
  return view;
}

// --- runtime service + logs -------------------------------------------------------

export type ServiceAction = "install" | "restart" | "uninstall" | "status";

export interface ServiceResult {
  ok: boolean;
  exitCode: number | null;
  timedOut: boolean;
  detail: string; // stderr (or stdout) tail, or the refusal
}

/** `replace`: the user confirmed replacing/removing a unit this app does not
 *  own (the Rust side refuses it otherwise). */
export async function runtimeService(
  invoke: InvokeFn,
  action: ServiceAction,
  opts: { env?: string[]; replace?: boolean } = {},
): Promise<ServiceResult> {
  try {
    const v = rec(await invoke("runtime_service", { action, env: opts.env ?? [], replace: opts.replace === true }));
    const stderr = str(v.stderr) ?? "";
    const stdout = str(v.stdout) ?? "";
    return {
      ok: v.ok === true,
      exitCode: num(v.exit_code),
      timedOut: v.timed_out === true,
      detail: stderr || stdout,
    };
  } catch (e) {
    return { ok: false, exitCode: null, timedOut: false, detail: String(e) };
  }
}

export async function openLog(invoke: InvokeFn, kind: "runtime" | "app"): Promise<{ ok: boolean; detail: string }> {
  try {
    return { ok: true, detail: String(await invoke("open_log", { kind })) };
  } catch (e) {
    return { ok: false, detail: String(e) };
  }
}

/** The one-time command (run on the bridge's machine) that turns a hand-run
 *  bridge into a managed one at `version` (the runtime's). */
export function managedBridgeCommand(version: string | null): string {
  return version ? `herdeck-service install bridge --managed --version ${version}` : "herdeck-service install bridge --managed";
}
