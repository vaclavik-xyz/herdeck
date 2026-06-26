// Framework-free parse / serialize / transport core for the config editor. A
// faithful sibling of deckClient.ts: pure functions + an injected `invoke`, so
// the whole client is unit-testable under Vitest without a Tauri WebView. The
// sidecar access token is NEVER here — the Rust shell injects it inside the
// token-free config_* commands (see src-tauri/src/lib.rs).

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
}

/** What `POST /config[/validate]` takes: the editable config minus `secrets`. */
export interface WriteBody {
  base: Record<string, unknown>;
  profiles: Record<string, Record<string, unknown>>;
  local: Record<string, unknown>;
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
  return { base: obj(v.base), profiles, local: obj(v.local), secrets, envLocked, activeProfile };
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
  return {
    base: clone(payload.base),
    profiles: clone(payload.profiles),
    local: clone(payload.local),
  };
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

/** The base `macros` list as editable records (always an array). */
export function macrosOf(payload: ConfigPayload): MacroRecord[] {
  const raw = payload.base.macros;
  if (!Array.isArray(raw)) return [];
  return raw.map((m) => {
    const r = obj(m);
    return { label: str(r.label), text: str(r.text) };
  });
}

function withMacros(payload: ConfigPayload, macros: MacroRecord[]): ConfigPayload {
  // Absent `macros` means "use DEFAULT_MACROS"; an empty list would disable them. So when
  // the last macro is removed, OMIT the key (return to defaults) rather than write `[]`.
  const base = { ...clone(payload.base) };
  if (macros.length === 0) delete base.macros;
  else base.macros = macros;
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
  const sec = asDict((payload.base as Record<string, unknown>).start_profiles);
  return Object.entries(sec).map(([name, argv]) => ({ name, argv: strList(argv) }));
}

/** The base `answer_profiles` map as editor rows, preserving `approve_always` absence. */
export function answerProfileRows(payload: ConfigPayload): AnswerProfileRow[] {
  const sec = asDict((payload.base as Record<string, unknown>).answer_profiles);
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

/** NEW payload with profile `name` removed. If `name` was the local active
 *  profile, that now-dangling selection is dropped from `local` too (so the next
 *  Apply doesn't write an unknown active profile); other local keys are kept.
 *  Input untouched. */
export function deleteProfile(payload: ConfigPayload, name: string): ConfigPayload {
  const profiles = clone(payload.profiles);
  delete profiles[name];
  let local = payload.local;
  if (asDict(payload.local).active_profile === name) {
    local = { ...clone(payload.local) };
    delete (local as Record<string, unknown>).active_profile;
  }
  return { ...payload, profiles, local };
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
