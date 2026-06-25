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
