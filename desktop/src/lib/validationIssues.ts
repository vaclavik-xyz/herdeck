import type { ConfigPayload } from "./configClient";

export type SettingsSection =
  | "servers"
  | "deck"
  | "view"
  | "theme"
  | "macros"
  | "start_profiles"
  | "notifications"
  | "safety"
  | "usage"
  | "answer_profiles"
  | "profiles"
  | "desktop";

export interface ValidationIssue {
  message: string;
  section: SettingsSection | null;
  fieldKey: string | null;
  owner: string | null;
  profileContext: string | null;
}

interface ValidationBody {
  context: string | null;
  body: string;
}

function validationBody(message: string): ValidationBody {
  const match = message.match(/^([^:]+):\s+(.+)$/);
  return match ? { context: match[1], body: match[2] } : { context: null, body: message };
}

type RoutingPayload = Pick<ConfigPayload, "activeProfile" | "base" | "profiles">;

function errorProfile(context: string | null, payload?: RoutingPayload): string | null {
  if (context == null) return null;
  const profile = context === "active" ? (payload?.activeProfile ?? "default") : context;
  return profile === "default" ? null : profile;
}

function issue(
  message: string,
  section: SettingsSection,
  fieldKey: string,
  profileContext: string | null,
  owner: string | null = null,
): ValidationIssue {
  return { message, section, fieldKey, owner, profileContext };
}

function serverSelectionOwner(serverId: string, profile: string | null, payload?: RoutingPayload): string | null {
  if (profile == null || payload == null) return null;
  const seen = new Set<string>();
  let current: string | null = profile;
  while (current && current !== "default" && !seen.has(current)) {
    seen.add(current);
    const overlay: Record<string, unknown> | undefined = payload.profiles[current];
    if (!overlay) return null;
    if (Array.isArray(overlay.servers) && overlay.servers.includes(serverId)) return current;
    current = typeof overlay.extends === "string" ? overlay.extends : "default";
  }
  return null;
}

function settingProfileOwner(
  profile: string | null,
  path: string[],
  payload?: RoutingPayload,
  parentIsEnough = false,
): string | null {
  if (profile == null || payload == null) return profile;
  const seen = new Set<string>();
  let current: string | null = profile;
  while (current && current !== "default" && !seen.has(current)) {
    seen.add(current);
    const overlay: Record<string, unknown> | undefined = payload.profiles[current];
    if (!overlay) return null;
    let value: unknown = overlay;
    const limit = parentIsEnough ? path.length - 1 : path.length;
    let found = true;
    for (let index = 0; index < limit; index += 1) {
      if (value == null || typeof value !== "object" || Array.isArray(value)
        || !Object.prototype.hasOwnProperty.call(value, path[index])) {
        found = false;
        break;
      }
      value = (value as Record<string, unknown>)[path[index]];
    }
    if (found) return current;
    current = typeof overlay.extends === "string" ? overlay.extends : "default";
  }
  return null;
}

export function classifyValidationIssue(message: string, payload?: RoutingPayload): ValidationIssue {
  const { context, body } = validationBody(message);
  const profileContext = errorProfile(context, payload);

  if (/^invalid grid\b/.test(body)) {
    return issue(message, "deck", "grid", settingProfileOwner(profileContext, ["deck", "grid"], payload));
  }

  const telegram = body.match(/notifications\.telegram\.([a-z_]+)/);
  if (telegram) return issue(
    message,
    "notifications",
    telegram[1],
    settingProfileOwner(profileContext, ["notifications", "telegram", telegram[1]], payload),
  );

  const usage = body.match(/usage\.([a-z_]+)/);
  if (usage) return issue(
    message,
    "usage",
    usage[1],
    settingProfileOwner(profileContext, ["usage", usage[1]], payload),
  );

  const hardware = body.match(/hardware\.([a-z_]+)/);
  if (hardware) return issue(message, "deck", hardware[1], null);

  const local = body.match(/local\.([a-z_]+)/);
  if (local) return issue(message, "deck", local[1], null);

  const tileToken = body.match(/unknown tile token .* in view\.([a-z_]+)/);
  if (tileToken) return issue(
    message,
    "view",
    tileToken[1],
    settingProfileOwner(profileContext, ["view", tileToken[1]], payload),
  );

  const view = body.match(/(?:unknown )?view\.([a-z_]+)/);
  if (view) return issue(
    message,
    "view",
    view[1],
    settingProfileOwner(profileContext, ["view", view[1]], payload),
  );

  const answerProfile = body.match(/^profile '([^']+)' missing '([a-z_]+)'/);
  if (answerProfile) return issue(
    message,
    "answer_profiles",
    answerProfile[2],
    settingProfileOwner(profileContext, ["answer_profiles", answerProfile[1], answerProfile[2]], payload, true),
    answerProfile[1],
  );

  const serverToken = body.match(/^env var .* for server '([^']+)' is not set/);
  if (serverToken) return issue(message, "servers", "token_env", null, serverToken[1]);

  const unknownServer = body.match(/^unknown server '([^']+)'/);
  if (unknownServer) {
    const owner = serverSelectionOwner(unknownServer[1], profileContext, payload);
    return owner
      ? issue(message, "profiles", "servers", null, owner)
      : issue(message, "deck", "overview_order", null);
  }

  if (/^unknown profile |^profile inheritance cycle/.test(body)) {
    return issue(message, "profiles", "extends", null, profileContext);
  }

  if (/^profile 'default' is reserved/.test(body)) return issue(message, "profiles", "name", null, "default");
  if (/^local must be a table|^hardware must be a table/.test(body)) return issue(message, "deck", "deck", null);

  return { message, section: null, fieldKey: null, owner: null, profileContext };
}

export function classifyValidationErrors(errors: string[], payload?: RoutingPayload): ValidationIssue[] {
  return errors.map((message) => classifyValidationIssue(message, payload));
}

export function fieldValidationKey(fieldKey: string, owner: string | null = null): string {
  return owner == null ? fieldKey : `${owner}\u0000${fieldKey}`;
}

export function messagesForSection(
  issues: ValidationIssue[],
  section: string,
  profileContext?: string | null,
): Record<string, string[]> {
  const grouped: Record<string, string[]> = {};
  for (const entry of issues) {
    if (entry.section !== section || entry.fieldKey == null) continue;
    if (profileContext !== undefined
      && entry.profileContext !== null
      && entry.profileContext !== profileContext) continue;
    const key = fieldValidationKey(entry.fieldKey, entry.owner);
    const messages = grouped[key] ?? (grouped[key] = []);
    if (!messages.includes(entry.message)) messages.push(entry.message);
  }
  return grouped;
}

export function revealValidationField(root: ParentNode, fieldKey: string, owner: string | null = null): boolean {
  const label = Array.from(root.querySelectorAll<HTMLElement>("[data-config-key]")).find(
    (element) => element.dataset.configKey === fieldKey
      && (owner == null || element.dataset.configOwner === owner),
  );
  if (!label) return false;

  let parent = label.parentElement;
  while (parent) {
    if (parent instanceof HTMLDetailsElement) parent.open = true;
    parent = parent.parentElement;
  }

  const field = label.closest<HTMLElement>(".field, .override, .tristate, .listfield") ?? label;
  field.scrollIntoView?.({ behavior: "smooth", block: "center" });
  field.querySelector<HTMLElement>("input, select, button")?.focus();
  return true;
}
