// Framework-free parse / serialize / transport core for the config editor. A
// faithful sibling of deckClient.ts: pure functions + an injected `invoke`, so
// the whole client is unit-testable under Vitest without a Tauri WebView. The
// sidecar access token is NEVER here — the Rust shell injects it inside the
// token-free config_* commands (see src-tauri/src/lib.rs).
import defaults from "./configDefaults.json";

/** A redacted secret flag: presence + where it resolves, never a value. */
export interface SecretFlag {
  set: boolean;
  source: "env" | "keychain" | null;
}

/** The parsed `GET /config` payload. `secrets` carries only presence flags. */
export interface ConfigPayload {
  base: Record<string, unknown>;
  profiles: Record<string, Record<string, unknown>>;
  local: Record<string, unknown>;
  secrets: Record<string, SecretFlag>;
  /** True iff the sidecar runs under a `HERDECK_PROFILE` env lock (řez 4b uses it). */
  envLocked: boolean;
  /** The effective active profile name (env > local > base > "default"). */
  activeProfile: string;
  /** Effective explicit renderer selection (env > local), or null for auto-detection. */
  runtimeDeck: string | null;
  /** Device-local Herdr sessions discovered by the sidecar. */
  localSessions: LocalSessionRecord[];
  /** On-disk content revision the payload was loaded from (staleness guard). */
  revision: string | null;
}

/** What `POST /config[/validate]` takes: the editable config minus `secrets`. */
export interface WriteBody {
  base: Record<string, unknown>;
  profiles: Record<string, Record<string, unknown>>;
  local: Record<string, unknown>;
  revision?: string;
}

function obj(v: unknown): Record<string, unknown> {
  return v != null && typeof v === "object" && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : {};
}

function parseSecretFlag(raw: unknown): SecretFlag {
  const v = obj(raw);
  const source = v.source === "env" || v.source === "keychain" ? v.source : null;
  return { set: v.set === true, source };
}

/** Shape a raw `/config` value into a ConfigPayload, or null when it is not an
 *  object. Missing sections default to `{}` (the onboarding / no-config case). */
export function parseConfig(raw: unknown): ConfigPayload | null {
  if (raw == null || typeof raw !== "object" || Array.isArray(raw)) return null;
  const v = raw as Record<string, unknown>;
  const profiles: Record<string, Record<string, unknown>> = {};
  for (const [name, overlay] of Object.entries(obj(v.profiles))) profiles[name] = obj(overlay);
  const secrets: Record<string, SecretFlag> = {};
  for (const [name, flag] of Object.entries(obj(v.secrets))) secrets[name] = parseSecretFlag(flag);
  const envLocked = v.env_locked === true;
  const activeProfile = typeof v.active_profile === "string" ? v.active_profile : "default";
  const runtimeDeck = typeof v.runtime_deck === "string" ? v.runtime_deck : null;
  const localSessions = Array.isArray(v.local_sessions)
    ? v.local_sessions.map(parseLocalSession).filter((item): item is LocalSessionRecord => item != null)
    : [];
  const revision = typeof v.revision === "string" ? v.revision : null;
  return { base: obj(v.base), profiles, local: obj(v.local), secrets, envLocked, activeProfile, runtimeDeck, localSessions, revision };
}

/** Extract the `errors` string list from a `{errors: [...]}` reply, dropping
 *  any non-string entry. Junk / missing → `[]`. */
export function parseValidate(raw: unknown): string[] {
  const v = obj(raw);
  if (!Array.isArray(v.errors)) return [];
  return v.errors.filter((e): e is string => typeof e === "string");
}

/** The Tauri `invoke` shape, injected so configClient stays framework-free. */
export type InvokeFn = (cmd: string, args?: Record<string, unknown>) => Promise<unknown>;

/** How the editor talks to the sidecar. Every call goes through a token-free
 *  Tauri command; the Rust shell injects the access token. Injectable so the
 *  editor is unit-testable with a fake. */
export interface ConfigTransport {
  read(): Promise<unknown>;
  validate(body: WriteBody): Promise<unknown>;
  write(body: WriteBody): Promise<unknown>;
  setActive(name: string): Promise<unknown>;
  setSecret(tokenEnv: string, value: string): Promise<number>;
  clearSecret(tokenEnv: string): Promise<number>;
}

/** Structured deep copy for the JSON-shaped config model (no functions/dates). */
function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v)) as T;
}

/** The editable config (no secrets), deep-copied so edits never alias the
 *  fetched payload. This is exactly what `POST /config[/validate]` takes. */
export function toWriteBody(payload: ConfigPayload): WriteBody {
  const body: WriteBody = {
    base: clone(payload.base),
    profiles: clone(payload.profiles),
    local: clone(payload.local),
  };
  if (payload.revision != null) body.revision = payload.revision;
  return body;
}

/** Update one base Telegram setting without rebuilding the table from the
 * editor's visible subset. This preserves advanced keys the current panel may
 * not render and treats blank/null as deleting only the edited field. */
export function updateBaseTelegram(
  payload: ConfigPayload,
  field: string,
  value: unknown,
): ConfigPayload {
  const current = obj(getAt(payload, "base", "notifications", "telegram"));
  const telegram: Record<string, unknown> = { ...clone(current) };
  const cleared = value == null || (typeof value === "string" && value.trim() === "");
  if (cleared) delete telegram[field];
  else telegram[field] = value;
  return Object.keys(telegram).length === 0
    ? removeAt(payload, "base", "notifications", "telegram")
    : setAt(payload, "base", "notifications", "telegram", telegram);
}

/** Is this validation error the write-rejected-as-stale signal? */
export function isStaleRevisionError(error: string): boolean {
  return error.startsWith("stale_revision");
}

/** The base value a profile overlay inherits for `section.key`, or undefined. */
export function inheritedValue(
  base: Record<string, unknown>,
  section: string,
  key: string,
): unknown {
  const sec = base[section];
  if (sec == null || typeof sec !== "object" || Array.isArray(sec)) return undefined;
  return (sec as Record<string, unknown>)[key];
}

/** New profiles map with `profiles[name][section][key] = value`. Input untouched. */
export function setOverride(
  profiles: Record<string, Record<string, unknown>>,
  name: string,
  section: string,
  key: string,
  value: unknown,
): Record<string, Record<string, unknown>> {
  const next = clone(profiles);
  const overlay = next[name] ?? (next[name] = {});
  const existing = overlay[section];
  const sec: Record<string, unknown> =
    existing != null && typeof existing === "object" && !Array.isArray(existing)
      ? (existing as Record<string, unknown>)
      : {};
  sec[key] = value;
  overlay[section] = sec;
  return next;
}

/** New profiles map with the overlay `section.key` removed; an emptied section
 *  is pruned so write() omits it. The (now-empty) profile entry is KEPT —
 *  deleting a profile is a separate explicit operation. Input untouched. */
export function clearOverride(
  profiles: Record<string, Record<string, unknown>>,
  name: string,
  section: string,
  key: string,
): Record<string, Record<string, unknown>> {
  const next = clone(profiles);
  const overlay = next[name];
  if (overlay == null) return next;
  const sec = overlay[section];
  if (sec != null && typeof sec === "object" && !Array.isArray(sec)) {
    const s = sec as Record<string, unknown>;
    delete s[key];
    if (Object.keys(s).length === 0) delete overlay[section];
  }
  return next;
}

/** The presence flag for `name`, defaulting to not-set. */
export function secretFlag(payload: ConfigPayload, name: string): SecretFlag {
  return payload.secrets[name] ?? { set: false, source: null };
}

/** A base server record as the editor edits it. */
export interface ServerRecord {
  id: string;
  url: string;
  token_env: string;
}

export interface LocalSessionRecord {
  name: string;
  server_id: string;
  socket_path: string;
  available: boolean;
  selected: boolean;
}

function parseLocalSession(raw: unknown): LocalSessionRecord | null {
  const item = obj(raw);
  if (
    typeof item.name !== "string"
    || typeof item.server_id !== "string"
    || typeof item.socket_path !== "string"
  ) {
    return null;
  }
  return {
    name: item.name,
    server_id: item.server_id,
    socket_path: item.socket_path,
    available: item.available === true,
    selected: item.selected === true,
  };
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/** The base `servers` list as editable records (always an array). */
export function serversOf(payload: ConfigPayload): ServerRecord[] {
  const raw = payload.base.servers;
  if (!Array.isArray(raw)) return [];
  return raw.map((s) => {
    const r = obj(s);
    return { id: str(r.id), url: str(r.url), token_env: str(r.token_env) };
  });
}

function withServers(payload: ConfigPayload, servers: ServerRecord[]): ConfigPayload {
  return { ...payload, base: { ...clone(payload.base), servers } };
}

/** NEW payload with a blank server appended. */
export function addServer(payload: ConfigPayload): ConfigPayload {
  return withServers(payload, [...serversOf(payload), { id: "", url: "", token_env: "" }]);
}

/** NEW payload with server `index` removed. */
export function removeServer(payload: ConfigPayload, index: number): ConfigPayload {
  const servers = serversOf(payload).filter((_, i) => i !== index);
  return withServers(payload, servers);
}

/** NEW payload with one field of server `index` set. */
export function updateServer(
  payload: ConfigPayload,
  index: number,
  field: keyof ServerRecord,
  value: string,
): ConfigPayload {
  const servers = serversOf(payload).map((s, i) => (i === index ? { ...s, [field]: value } : s));
  return withServers(payload, servers);
}

/** Select or deselect one discovered local Herdr session in local.toml. */
export function setLocalSessionSelected(
  payload: ConfigPayload,
  name: string,
  selected: boolean,
): ConfigPayload {
  const current = payload.localSessions
    .filter((session) => session.selected)
    .map((session) => session.name);
  const names = selected
    ? [...new Set([...current, name])]
    : current.filter((item) => item !== name);
  const next = setAt(payload, "local", "local", "herdr_sessions", names);
  return {
    ...next,
    localSessions: payload.localSessions.map((session) =>
      session.name === name ? { ...session, selected } : session
    ),
  };
}

/** Editor root: the base config, or the machine-local config (`local.toml`). */
export type ConfigRoot = "base" | "local";

function asDict(v: unknown): Record<string, unknown> {
  return v != null && typeof v === "object" && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : {};
}

/** Read `payload[root][section][key]`, or undefined when any level is absent. */
export function getAt(
  payload: ConfigPayload,
  root: ConfigRoot,
  section: string,
  key: string,
): unknown {
  return asDict(asDict(payload[root])[section])[key];
}

/** NEW payload with `payload[root][section][key] = value`. Input untouched;
 *  intermediate root/section objects are created as needed. */
export function setAt(
  payload: ConfigPayload,
  root: ConfigRoot,
  section: string,
  key: string,
  value: unknown,
): ConfigPayload {
  const rootObj = clone(asDict(payload[root]));
  const sec = { ...asDict(rootObj[section]) };
  sec[key] = value;
  rootObj[section] = sec;
  return { ...payload, [root]: rootObj };
}

/** NEW payload with `payload[root][section][key]` deleted. The (possibly now
 *  empty) section dict is left in place. Input untouched. */
export function removeAt(
  payload: ConfigPayload,
  root: ConfigRoot,
  section: string,
  key: string,
): ConfigPayload {
  const rootObj = clone(asDict(payload[root]));
  const existing = rootObj[section];
  if (existing != null && typeof existing === "object" && !Array.isArray(existing)) {
    const sec = { ...(existing as Record<string, unknown>) };
    delete sec[key];
    rootObj[section] = sec;
  }
  return { ...payload, [root]: rootObj };
}

/** A base macro record as the editor edits it. */
export interface MacroRecord {
  label: string;
  text: string;
}

/** Extract `{label,text}[]` from any list value (tolerates junk entries). */
export function macroRecords(raw: unknown): MacroRecord[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((m) => {
    const r = obj(m);
    return { label: str(r.label), text: str(r.text) };
  });
}

/** The effective base `macros` list as editable records. An absent/null value
 *  means the backend defaults, while an explicit empty list disables macros. */
export function macrosOf(payload: ConfigPayload): MacroRecord[] {
  return payload.base.macros == null
    ? DEFAULT_MACROS.map((m) => ({ ...m }))
    : macroRecords(payload.base.macros);
}

// Shared defaults are loaded from configDefaults.json. A Python contract test
// compares that file with the backend dataclasses/constants.
export const DEFAULT_START_PROFILES: Record<string, string[]> = clone(defaults.start_profiles);
export const DEFAULT_MACROS: MacroRecord[] = clone(defaults.macros);
export const DEFAULT_ANSWER_PROFILES: Record<string, Record<string, string[]>> = clone(defaults.answer_profiles);

function withMacros(payload: ConfigPayload, macros: MacroRecord[]): ConfigPayload {
  // Once the user edits the effective list, materialize it — including `[]`,
  // which is the backend's explicit opt-out rather than "restore defaults".
  const base = { ...clone(payload.base) };
  base.macros = macros;
  return { ...payload, base };
}

/** NEW payload with a blank macro appended. */
export function addMacro(payload: ConfigPayload): ConfigPayload {
  return withMacros(payload, [...macrosOf(payload), { label: "", text: "" }]);
}

/** NEW payload with macro `index` removed. */
export function removeMacro(payload: ConfigPayload, index: number): ConfigPayload {
  return withMacros(payload, macrosOf(payload).filter((_, i) => i !== index));
}

/** NEW payload with one field of macro `index` set. */
export function updateMacro(
  payload: ConfigPayload,
  index: number,
  field: keyof MacroRecord,
  value: string,
): ConfigPayload {
  const macros = macrosOf(payload).map((m, i) => (i === index ? { ...m, [field]: value } : m));
  return withMacros(payload, macros);
}

// --- map-section serialization core (used by Start/Answer profile sections) ---

/** A start-profile editor row. */
export interface StartProfileRow {
  name: string;
  argv: string[];
}

/** An answer-profile editor row. `approve_always: null` = the key was ABSENT, which the
 *  backend treats as "fall back to approve"; preserved so an unrelated edit never writes
 *  `[]` (which would mean "no approve-always keys" — a silent semantics change). */
export interface AnswerProfileRow {
  name: string;
  approve: string[];
  deny: string[];
  stop: string[];
  approve_always: string[] | null;
}

function strList(v: unknown): string[] {
  return Array.isArray(v) ? v.map(String) : [];
}

/** The base `start_profiles` map (`name → argv`) as editor rows. */
export function startProfileRows(payload: ConfigPayload): StartProfileRow[] {
  const raw = (payload.base as Record<string, unknown>).start_profiles;
  const sec = raw == null ? DEFAULT_START_PROFILES : asDict(raw);
  return Object.entries(sec).map(([name, argv]) => ({ name, argv: strList(argv) }));
}

/** The base `answer_profiles` map as editor rows, preserving `approve_always` absence. */
export function answerProfileRows(payload: ConfigPayload): AnswerProfileRow[] {
  const sec = {
    ...DEFAULT_ANSWER_PROFILES,
    ...asDict((payload.base as Record<string, unknown>).answer_profiles),
  };
  return Object.entries(sec).map(([name, raw]) => {
    const o = asDict(raw);
    return {
      name,
      approve: strList(o.approve),
      deny: strList(o.deny),
      stop: strList(o.stop),
      approve_always: "approve_always" in o ? strList(o.approve_always) : null,
    };
  });
}

/** Serialize named rows into a map section. Blank names are skipped; a repeated name sets
 *  `duplicate`; with no named rows the section is `undefined` so the caller OMITS the key
 *  (never writes `{}`, which would disable backend defaults). */
export function serializeNamedRows<R extends { name: string }, V>(
  rows: R[],
  toValue: (row: R) => V,
): { duplicate: boolean; section: Record<string, V> | undefined } {
  const named = rows.map((r) => r.name.trim()).filter((n) => n !== "");
  const duplicate = new Set(named).size !== named.length;
  const section: Record<string, V> = {};
  for (const r of rows) {
    const n = r.name.trim();
    if (n !== "") section[n] = toValue(r);
  }
  return { duplicate, section: Object.keys(section).length > 0 ? section : undefined };
}

/** NEW payload with `base[section]` set to `serialized` (or the key DELETED when
 *  `serialized` is `undefined` — absent means "use backend defaults", unlike `{}`), or
 *  `null` when the serialized section is unchanged (so the caller skips marking dirty). */
export function applyMapSection(
  payload: ConfigPayload,
  section: string,
  serialized: Record<string, unknown> | undefined,
): ConfigPayload | null {
  const current = (payload.base as Record<string, unknown>)[section];
  const currentSig = current === undefined ? "" : JSON.stringify(current);
  const nextSig = serialized === undefined ? "" : JSON.stringify(serialized);
  if (currentSig === nextSig) return null;
  const base = { ...(payload.base as Record<string, unknown>) };
  if (serialized === undefined) delete base[section];
  else base[section] = serialized;
  return { ...payload, base };
}

/** NEW payload writing `[root][section][key] = list`, or DELETING that key when `list` is
 *  empty. In this config an ABSENT list key means "use the backend default" (e.g.
 *  `DEFAULT_BOTTOM_ROW`, all servers for `overview_order`); writing an explicit `[]` would
 *  instead mean "none" and silently disable that default. So řez 4a maps an emptied list
 *  editor to "return to default" (omit the key). Authoring an INTENTIONAL explicit-empty
 *  list (e.g. `view.tile_primary = []` to switch a tile line off) is řez 4b's presence-aware
 *  override UX — out of scope here. */
export function putList(
  payload: ConfigPayload,
  root: ConfigRoot,
  section: string,
  key: string,
  list: string[],
): ConfigPayload {
  return list.length === 0
    ? removeAt(payload, root, section, key)
    : setAt(payload, root, section, key, list);
}

/** The tri-state of a list key: absent → "default" (backend default applies),
 *  `[]` → "empty" (explicit none, default disabled), non-empty → "custom". */
export type ListFieldState = "default" | "custom" | "empty";

/** Read a list key's tri-state. A missing key (any level absent) is "default";
 *  an empty array is "empty"; anything else present is "custom". */
export function listFieldState(
  payload: ConfigPayload,
  root: ConfigRoot,
  section: string,
  key: string,
): ListFieldState {
  const v = getAt(payload, root, section, key);
  if (v === undefined) return "default";
  return Array.isArray(v) && v.length === 0 ? "empty" : "custom";
}

/** NEW payload writing the chosen tri-state for a list key: "default" OMITS the
 *  key (removeAt → backend default), "empty" writes an explicit `[]`, "custom"
 *  writes `list` (a "custom" list that is empty is written as `[]` and reads back
 *  as "empty"). Composes the tested setAt/removeAt; input untouched. */
export function setListField(
  payload: ConfigPayload,
  root: ConfigRoot,
  section: string,
  key: string,
  state: ListFieldState,
  list: string[],
): ConfigPayload {
  if (state === "default") return removeAt(payload, root, section, key);
  if (state === "empty") return setAt(payload, root, section, key, []);
  return setAt(payload, root, section, key, list);
}

// --- profile overlay resolution (řez 4b-ii-β1) ---

/** Read a path under `root`; returns whether every level was present + the value. */
function readPath(root: unknown, path: string[]): { found: boolean; value: unknown } {
  let cur: unknown = root;
  for (const k of path) {
    if (cur == null || typeof cur !== "object" || Array.isArray(cur) || !(k in (cur as Record<string, unknown>))) {
      return { found: false, value: undefined };
    }
    cur = (cur as Record<string, unknown>)[k];
  }
  return { found: true, value: cur };
}

/** Overlay dicts a profile INHERITS: base-most parent down to but EXCLUDING `profile`
 *  itself (the overlays via `extends`). Mirrors backend `_profile_overlays` minus the
 *  profile's own overlay. A cycle or unknown target stops the walk (editor falls back to
 *  base; backend rejects on write). */
function inheritedChain(
  profiles: Record<string, Record<string, unknown>>,
  profile: string,
): Record<string, unknown>[] {
  const chain: string[] = [];
  // Seed seen with the starting profile so any re-entry is detected as a cycle.
  const seen = new Set<string>([profile]);
  const ext0 = asDict(profiles[profile]).extends;
  let cur: string | undefined = typeof ext0 === "string" ? ext0 : undefined;
  while (cur && cur !== "default") {
    if (!(cur in profiles)) return []; // unknown target: discard chain, fall back to base
    if (seen.has(cur)) return [];     // cycle detected: discard chain, fall back to base
    seen.add(cur);
    chain.push(cur);
    const ext = asDict(profiles[cur]).extends;
    cur = typeof ext === "string" ? ext : undefined;
  }
  return chain.reverse().map((n) => asDict(profiles[n]));
}

/** The value `profile` inherits at `path` (base + parent overlays via extends, excluding
 *  the profile's own overlay), or undefined when absent everywhere. */
export function inheritedForPath(payload: ConfigPayload, profile: string, path: string[]): unknown {
  let value = readPath(payload.base, path).value;
  for (const overlay of inheritedChain(payload.profiles, profile)) {
    const r = readPath(overlay, path);
    if (r.found) value = r.value;
  }
  return value;
}

/** Chain-aware inherited value for `section.key` (the common 2-level case). */
export function inheritedFor(payload: ConfigPayload, profile: string, section: string, key: string): unknown {
  return inheritedForPath(payload, profile, [section, key]);
}

/** The raw value at `path` in `profile`'s OWN overlay (no inheritance), or undefined. */
export function overrideValuePath(payload: ConfigPayload, profile: string, path: string[]): unknown {
  return readPath(payload.profiles[profile], path).value;
}

/** The raw override value for `section.key` in `profile`'s overlay, or undefined. */
export function overrideValue(payload: ConfigPayload, profile: string, section: string, key: string): unknown {
  return overrideValuePath(payload, profile, [section, key]);
}

/** The EFFECTIVE UI/deck language for the active profile: the profile's own
 *  `view.language` override, else the inherited (extends-chain + base) value,
 *  else "en" — mirroring the backend's profile merge for this key. */
export function effectiveLanguage(payload: ConfigPayload): "en" | "cs" {
  const prof = payload.activeProfile;
  const v =
    prof !== "default" && payload.profiles[prof] != null
      ? (overrideValue(payload, prof, "view", "language") ??
        inheritedFor(payload, prof, "view", "language"))
      : readPath(payload.base, ["view", "language"]).value;
  return v === "cs" ? "cs" : "en";
}

/** Override state of `section.key` in `profile`'s overlay: absent → "default" (= inherit),
 *  `[]` → "empty", anything else present → "custom". Reuses `ListFieldState`; in overlay
 *  context "default" denotes inheritance. */
export function overrideState(payload: ConfigPayload, profile: string, section: string, key: string): ListFieldState {
  const { found, value } = readPath(payload.profiles[profile], [section, key]);
  if (!found) return "default";
  return Array.isArray(value) && value.length === 0 ? "empty" : "custom";
}

/** JS mirror of backend `settings._merge_section`: two dicts merge per-key
 *  recursively; a list/scalar overlay (or absent base) replaces wholesale. */
export function mergeSection(base: unknown, overlay: unknown): unknown {
  if (
    base != null && typeof base === "object" && !Array.isArray(base) &&
    overlay != null && typeof overlay === "object" && !Array.isArray(overlay)
  ) {
    const out: Record<string, unknown> = { ...(base as Record<string, unknown>) };
    for (const [k, v] of Object.entries(overlay as Record<string, unknown>)) {
      out[k] = mergeSection(out[k], v);
    }
    return out;
  }
  return overlay;
}

/** Raw chain merge of `section` a `profile` INHERITS: base merged with parent overlays
 *  via `extends` (per-key, mirroring the backend), EXCLUDING the profile's OWN overlay.
 *  A cycle/unknown extends target falls back to base (via `inheritedChain`). `present` is
 *  whether ANY level set the section — distinguishes "absent everywhere" (→ backend default)
 *  from an explicit `{}` (→ none). Defaults are applied by the resolvers below, NOT here. */
export function inheritedSection(
  payload: ConfigPayload,
  profile: string,
  section: string,
): { present: boolean; map: Record<string, unknown> } {
  const base = asDict(payload.base);
  let present = section in base;
  let merged: unknown = base[section];
  for (const overlay of inheritedChain(payload.profiles, profile)) {
    if (section in overlay) {
      merged = mergeSection(present ? merged : undefined, overlay[section]);
      present = true;
    }
  }
  return { present, map: asDict(merged) };
}

/** Effective inherited `start_profiles` map: absent everywhere → DEFAULT_START_PROFILES
 *  (backend `_launcher(None)`); explicit `{}` → none; otherwise the merged map. */
export function inheritedStartProfiles(payload: ConfigPayload, profile: string): Record<string, unknown> {
  const { present, map } = inheritedSection(payload, profile, "start_profiles");
  return present ? map : { ...DEFAULT_START_PROFILES };
}

/** Effective inherited `macros` list: absent → DEFAULT_MACROS (backend `_macro_set(None)`);
 *  `[]` → none; otherwise the inherited list. (macros is a LIST → use inheritedForPath.) */
export function inheritedMacros(payload: ConfigPayload, profile: string): MacroRecord[] {
  const v = inheritedForPath(payload, profile, ["macros"]);
  return v === undefined ? DEFAULT_MACROS.map((m) => ({ ...m })) : macroRecords(v);
}

/** Effective inherited `answer_profiles` map: DEFAULT_ANSWER_PROFILES are ALWAYS the base
 *  (backend `dict(DEFAULT_PROFILES)`), config overriding whole entries per-name. Shallow
 *  per-entry spread = whole-entry replace, faithful to backend `_build_config`. */
export function inheritedAnswerProfiles(payload: ConfigPayload, profile: string): Record<string, unknown> {
  const { present, map } = inheritedSection(payload, profile, "answer_profiles");
  return { ...DEFAULT_ANSWER_PROFILES, ...(present ? map : {}) };
}

/** Override state at `path` in `profile`'s OWN overlay (path variant of `overrideState`):
 *  absent → "default" (= inherit), `[]` → "empty", anything else present → "custom". */
export function overrideStatePath(
  payload: ConfigPayload,
  profile: string,
  path: string[],
): ListFieldState {
  const { found, value } = readPath(payload.profiles[profile], path);
  if (!found) return "default";
  return Array.isArray(value) && value.length === 0 ? "empty" : "custom";
}

/** NEW profiles map writing `profiles[name]<path> = value`, creating nested dicts as
 *  needed. Input untouched. */
export function setOverridePath(
  profiles: Record<string, Record<string, unknown>>,
  name: string,
  path: string[],
  value: unknown,
): Record<string, Record<string, unknown>> {
  const next = clone(profiles);
  let cur: Record<string, unknown> = next[name] ?? (next[name] = {});
  for (let i = 0; i < path.length - 1; i++) {
    const k = path[i];
    const child = cur[k];
    if (child == null || typeof child !== "object" || Array.isArray(child)) cur[k] = {};
    cur = cur[k] as Record<string, unknown>;
  }
  cur[path[path.length - 1]] = value;
  return next;
}

/** NEW profiles map with `profiles[name]<path>` removed; emptied ancestor dicts are pruned
 *  up to (but not including) the profile entry, which is kept. Input untouched. */
export function clearOverridePath(
  profiles: Record<string, Record<string, unknown>>,
  name: string,
  path: string[],
): Record<string, Record<string, unknown>> {
  const next = clone(profiles);
  const stack: Record<string, unknown>[] = [];
  let cur = next[name] as Record<string, unknown> | undefined;
  if (cur == null) return next;
  for (let i = 0; i < path.length - 1; i++) {
    stack.push(cur);
    const child = cur[path[i]];
    if (child == null || typeof child !== "object" || Array.isArray(child)) return next; // path absent
    cur = child as Record<string, unknown>;
  }
  stack.push(cur);
  delete cur[path[path.length - 1]];
  for (let i = stack.length - 1; i >= 1; i--) {
    if (Object.keys(stack[i]).length === 0) delete stack[i - 1][path[i - 1]];
    else break;
  }
  return next;
}

// --- profiles (řez 4b-i) ---

/** Result of a profile mutation that can fail validation. */
export type ProfileResult =
  | { ok: true; payload: ConfigPayload }
  | { ok: false; error: string };

/** The named profile keys (the implicit base is "default", never listed here). */
export function profileNames(payload: ConfigPayload): string[] {
  return Object.keys(payload.profiles);
}

/** NEW payload with an empty profile `name`, or an error when the trimmed name is
 *  blank, the reserved "default", or already taken. Input untouched. */
export function createProfile(payload: ConfigPayload, name: string): ProfileResult {
  const n = name.trim();
  if (n === "") return { ok: false, error: "jméno profilu nesmí být prázdné" };
  if (n === "default") return { ok: false, error: "'default' je rezervováno pro bázi" };
  if (n in payload.profiles) return { ok: false, error: `profil '${n}' už existuje` };
  const profiles = { ...clone(payload.profiles), [n]: {} };
  return { ok: true, payload: { ...payload, profiles } };
}

/** NEW payload with profile `name` removed. Any PERSISTED active selector pointing
 *  at the deleted profile is also cleared so the next Apply doesn't write an unknown
 *  active profile (backend would reject it): both the local selector
 *  (`local.active_profile`) AND the legacy top-level one (`base.active_profile`,
 *  carried into base by řez-4a `read()`). Other keys in each are kept. An env lock
 *  (`HERDECK_PROFILE`) can't be cleared here — `ProfilesSection` blocks deleting the
 *  env-locked active profile (Task 4). Input untouched. */
export function deleteProfile(payload: ConfigPayload, name: string): ConfigPayload {
  const profiles = clone(payload.profiles);
  delete profiles[name];
  let local = payload.local;
  if (asDict(payload.local).active_profile === name) {
    local = { ...clone(payload.local) };
    delete (local as Record<string, unknown>).active_profile;
  }
  let base = payload.base;
  if (asDict(payload.base).active_profile === name) {
    base = { ...clone(payload.base) };
    delete (base as Record<string, unknown>).active_profile;
  }
  const activeProfile = payload.activeProfile === name ? "default" : payload.activeProfile;
  return { ...payload, profiles, local, base, activeProfile };
}

/** The `extends` target of profile `name` ("default" = inherit base, when absent). */
export function profileExtends(payload: ConfigPayload, name: string): string {
  const ext = asDict(payload.profiles[name]).extends;
  return typeof ext === "string" ? ext : "default";
}

/** NEW payload with profile `name`'s `extends` set. A scalar — "default" is written
 *  literally (equals the default), so it is exempt from absent≠empty. Input untouched. */
export function setProfileExtends(
  payload: ConfigPayload,
  name: string,
  extendsName: string,
): ConfigPayload {
  const profiles = clone(payload.profiles);
  const overlay = { ...asDict(profiles[name]) };
  overlay.extends = extendsName;
  profiles[name] = overlay;
  return { ...payload, profiles };
}

/** Profile `name`'s `servers` list (absent → []). */
export function profileServers(payload: ConfigPayload, name: string): string[] {
  return strList(asDict(payload.profiles[name]).servers);
}

/** NEW payload with profile `name`'s `servers` set, or the key OMITTED when the list
 *  is empty. An absent `servers` means "inherit base servers"; an explicit `[]` (a
 *  serverless profile) is řez 4b-ii's presence-aware authoring — out of scope here.
 *  Input untouched. */
export function setProfileServers(
  payload: ConfigPayload,
  name: string,
  servers: string[],
): ConfigPayload {
  const profiles = clone(payload.profiles);
  const overlay = { ...asDict(profiles[name]) };
  if (servers.length === 0) delete overlay.servers;
  else overlay.servers = servers;
  profiles[name] = overlay;
  return { ...payload, profiles };
}

/** Base `[section]` tri-state: absent → "default" (backend default map), `{}` → "empty"
 *  (explicit none, e.g. no launchers), non-empty dict → "custom". */
export function mapSectionState(payload: ConfigPayload, section: string): ListFieldState {
  const v = (payload.base as Record<string, unknown>)[section];
  if (v === undefined) return "default";
  if (v != null && typeof v === "object" && !Array.isArray(v)) {
    return Object.keys(v as Record<string, unknown>).length === 0 ? "empty" : "custom";
  }
  return "custom";
}

/** NEW payload setting base `[section]` map state: "default" DELETES the key (backend
 *  default), "empty" writes `{}` (explicit none), "custom" is a no-op (the rows editor
 *  populates the map). Input untouched. */
export function setMapSectionState(payload: ConfigPayload, section: string, state: ListFieldState): ConfigPayload {
  if (state === "custom") return payload;
  const base = { ...(payload.base as Record<string, unknown>) };
  if (state === "default") delete base[section];
  else base[section] = {};
  return { ...payload, base };
}

/** Whether profile `name` has an explicit `servers` selection (present, incl. `[]` =
 *  serverless) or inherits base servers (key absent). */
export function profileServersState(payload: ConfigPayload, name: string): "inherit" | "explicit" {
  return "servers" in asDict(payload.profiles[name]) ? "explicit" : "inherit";
}

/** NEW payload writing profile `name`'s `servers` ALWAYS (even `[]` = serverless),
 *  unlike `setProfileServers` which omits an empty list. Input untouched. */
export function setProfileServersExplicit(payload: ConfigPayload, name: string, servers: string[]): ConfigPayload {
  const profiles = clone(payload.profiles);
  const overlay = { ...asDict(profiles[name]) };
  overlay.servers = servers;
  profiles[name] = overlay;
  return { ...payload, profiles };
}

/** NEW payload OMITTING profile `name`'s `servers` key (back to inheriting base). Input untouched. */
export function clearProfileServers(payload: ConfigPayload, name: string): ConfigPayload {
  const profiles = clone(payload.profiles);
  const overlay = { ...asDict(profiles[name]) };
  delete overlay.servers;
  profiles[name] = overlay;
  return { ...payload, profiles };
}

/** The servers a profile EFFECTIVELY inherits when it does NOT override `servers` itself —
 *  mirrors backend `settings._build_config` selection resolution: the nearest parent profile in
 *  the `extends` chain that sets `servers` wins; else the profile's merged `deck.overview_order`
 *  (base + parents + the profile's own deck overlay); else all base server ids. Used to seed the
 *  editor when toggling a profile to an explicit selection, so it starts from what the profile
 *  already had — not an empty list (which would silently make it serverless). */
export function effectiveProfileServers(payload: ConfigPayload, name: string): string[] {
  // 1. nearest parent profile that sets `servers` (chain is base-most → parent; reverse so the
  //    most-derived parent wins, matching the backend's last-overlay-wins assignment).
  for (const overlay of [...inheritedChain(payload.profiles, name)].reverse()) {
    if (Array.isArray(overlay.servers)) return (overlay.servers as unknown[]).map(String);
  }
  // 2. the profile's effective deck.overview_order (base + parents + the profile's own deck overlay).
  const inhDeck = inheritedSection(payload, name, "deck").map;
  const effDeck = asDict(mergeSection(inhDeck, asDict(asDict(payload.profiles[name]).deck)));
  if (Array.isArray(effDeck.overview_order)) return (effDeck.overview_order as unknown[]).map(String);
  // 3. all base server ids.
  return serversOf(payload).map((s) => s.id).filter((id) => id !== "");
}

/** Every non-blank `token_env` string referenced anywhere in base + profiles
 *  (servers, base/profile telegram, profile overlays). Mirrors the backend's
 *  `_collect_token_envs` so the editor can spot keychain entries gone orphan. */
export function referencedTokenEnvs(payload: ConfigPayload): Set<string> {
  const out = new Set<string>();
  const walk = (v: unknown): void => {
    if (Array.isArray(v)) {
      v.forEach(walk);
    } else if (v != null && typeof v === "object") {
      for (const [k, val] of Object.entries(v as Record<string, unknown>)) {
        if (k === "token_env" && typeof val === "string" && val !== "") out.add(val);
        else walk(val);
      }
    }
  };
  walk(payload.base);
  walk(payload.profiles);
  return out;
}

/** Keychain-backed secrets no `token_env` in the config still references — cleanup
 *  candidates after a rename/delete. env-sourced secrets (we can't clear them) and
 *  unset ones (nothing to clear) are excluded. */
export function orphanedSecrets(payload: ConfigPayload): string[] {
  const referenced = referencedTokenEnvs(payload);
  return Object.entries(payload.secrets)
    .filter(([name, flag]) => flag.set && flag.source === "keychain" && !referenced.has(name))
    .map(([name]) => name);
}

/** Parse the `{changed: bool}` reply from `config_set_active`. */
export function parseActiveChanged(raw: unknown): boolean {
  return obj(raw).changed === true;
}

/** The default deck-toggle global hotkey. Cross-platform: CmdOrCtrl maps to
 *  Cmd on macOS and Ctrl elsewhere. */
export const DEFAULT_TOGGLE_DECK_HOTKEY = "CmdOrCtrl+Shift+D";

/** The configured deck-toggle accelerator for the editor field. An ABSENT key
 *  shows the default; an explicit "" (disabled) is returned verbatim so the
 *  field can show it empty. Mirrors the Rust semantics (missing=default, ""=off). */
export function toggleDeckHotkey(payload: ConfigPayload): string {
  const v = getAt(payload, "base", "hotkeys", "toggle_deck");
  return typeof v === "string" ? v : DEFAULT_TOGGLE_DECK_HOTKEY;
}

/** NEW payload with base.hotkeys.toggle_deck set (incl. "" to disable). */
export function setToggleDeckHotkey(payload: ConfigPayload, value: string): ConfigPayload {
  return setAt(payload, "base", "hotkeys", "toggle_deck", value);
}

/** The three deck window modes (matches Rust `WindowMode::as_str`). */
export const WINDOW_MODES = ["normal", "floating", "always_on_top"] as const;
export type WindowMode = (typeof WINDOW_MODES)[number];

/** Default deck window mode. Mirrors Rust `parse_window_mode` (missing → Normal). */
export const DEFAULT_WINDOW_MODE: WindowMode = "normal";

/** The configured deck window mode. An ABSENT or unknown value → the default,
 *  mirroring the Rust parser (missing/garbage → Normal). */
export function windowMode(payload: ConfigPayload): WindowMode {
  const v = getAt(payload, "base", "desktop", "window_mode");
  return typeof v === "string" && (WINDOW_MODES as readonly string[]).includes(v)
    ? (v as WindowMode)
    : DEFAULT_WINDOW_MODE;
}

/** NEW payload with base.desktop.window_mode set. */
export function setWindowMode(payload: ConfigPayload, value: WindowMode): ConfigPayload {
  return setAt(payload, "base", "desktop", "window_mode", value);
}

export function commandTransport(invoke: InvokeFn): ConfigTransport {
  const asCode = (v: unknown) => (typeof v === "number" ? v : 0);
  return {
    read: () => invoke("config_read"),
    validate: (body) => invoke("config_validate", { body }),
    write: (body) => invoke("config_write", { body }),
    setActive: (name) => invoke("config_set_active", { name }),
    async setSecret(tokenEnv, value) {
      return asCode(await invoke("config_secret_set", { tokenEnv, value }));
    },
    async clearSecret(tokenEnv) {
      return asCode(await invoke("config_secret_clear", { tokenEnv }));
    },
  };
}

/** Czech-pluralized validation-error count for the savebar badge. */
export function errorCountLabel(n: number, lang: "en" | "cs" = "en"): string {
  if (lang === "cs") {
    if (n === 1) return "1 chyba";
    if (n >= 2 && n <= 4) return `${n} chyby`;
    return `${n} chyb`;
  }
  return n === 1 ? "1 error" : `${n} errors`;
}
