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
}

interface ValidationBody {
  context: string | null;
  body: string;
}

function validationBody(message: string): ValidationBody {
  const match = message.match(/^([^:]+):\s+(.+)$/);
  return match ? { context: match[1], body: match[2] } : { context: null, body: message };
}

function issue(message: string, section: SettingsSection, fieldKey: string): ValidationIssue {
  return { message, section, fieldKey };
}

export function classifyValidationIssue(message: string, activeProfile = "default"): ValidationIssue {
  const { context, body } = validationBody(message);

  if (/^invalid grid\b/.test(body)) return issue(message, "deck", "grid");

  const telegram = body.match(/notifications\.telegram\.([a-z_]+)/);
  if (telegram) return issue(message, "notifications", telegram[1]);

  const usage = body.match(/usage\.([a-z_]+)/);
  if (usage) return issue(message, "usage", usage[1]);

  const hardware = body.match(/hardware\.([a-z_]+)/);
  if (hardware) return issue(message, "deck", hardware[1]);

  const local = body.match(/local\.([a-z_]+)/);
  if (local) return issue(message, "deck", local[1]);

  const tileToken = body.match(/unknown tile token .* in view\.([a-z_]+)/);
  if (tileToken) return issue(message, "view", tileToken[1]);

  const view = body.match(/(?:unknown )?view\.([a-z_]+)/);
  if (view) return issue(message, "view", view[1]);

  const answerProfile = body.match(/^profile '[^']+' missing '([a-z_]+)'/);
  if (answerProfile) return issue(message, "answer_profiles", answerProfile[1]);

  if (/^env var .* for server /.test(body)) return issue(message, "servers", "token_env");

  if (/^unknown server /.test(body)) {
    const baseContext = context === "active" && activeProfile === "default";
    return baseContext
      ? issue(message, "deck", "overview_order")
      : issue(message, "profiles", "servers");
  }

  if (/^unknown profile |^profile inheritance cycle/.test(body)) {
    return issue(message, "profiles", "extends");
  }

  if (/^profile 'default' is reserved/.test(body)) return issue(message, "profiles", "name");
  if (/^local must be a table|^hardware must be a table/.test(body)) return issue(message, "deck", "deck");

  return { message, section: null, fieldKey: null };
}

export function classifyValidationErrors(errors: string[], activeProfile = "default"): ValidationIssue[] {
  return errors.map((message) => classifyValidationIssue(message, activeProfile));
}

export function messagesForSection(issues: ValidationIssue[], section: string): Record<string, string[]> {
  const grouped: Record<string, string[]> = {};
  for (const entry of issues) {
    if (entry.section !== section || entry.fieldKey == null) continue;
    const messages = grouped[entry.fieldKey] ?? (grouped[entry.fieldKey] = []);
    if (!messages.includes(entry.message)) messages.push(entry.message);
  }
  return grouped;
}

export function revealValidationField(root: ParentNode, fieldKey: string): boolean {
  const label = Array.from(root.querySelectorAll<HTMLElement>("[data-config-key]")).find(
    (element) => element.dataset.configKey === fieldKey,
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
