import { describe, expect, it, vi } from "vitest";
import {
  classifyValidationErrors,
  classifyValidationIssue,
  messagesForSection,
  revealValidationField,
} from "./validationIssues";

describe("validation issue routing", () => {
  it.each([
    ["active: invalid grid 'wide', expected e.g. '5x3'", "deck", "grid"],
    ["work: notifications.telegram.allowed_user_ids must contain integers", "notifications", "allowed_user_ids"],
    ["active: unknown tile token 'bogus' in view.tile_primary", "view", "tile_primary"],
    ["active: usage.refresh_secs must be an integer >= 30", "usage", "refresh_secs"],
    ["active: hardware.brightness must be an integer from 0 to 100", "deck", "brightness"],
    ["night: profile 'codex' missing 'deny'", "answer_profiles", "deny"],
    ["active: env var 'REMOTE_TOKEN' for server 'bench' is not set", "servers", "token_env"],
  ])("routes %s", (message, section, fieldKey) => {
    expect(classifyValidationIssue(message)).toMatchObject({ section, fieldKey });
  });

  it("distinguishes base ordering from a profile server selection", () => {
    expect(classifyValidationIssue("active: unknown server 'ghost'", "default")).toMatchObject({
      section: "deck", fieldKey: "overview_order",
    });
    expect(classifyValidationIssue("work: unknown server 'ghost'", "work")).toMatchObject({
      section: "profiles", fieldKey: "servers",
    });
  });

  it("groups and deduplicates only the active section", () => {
    const issues = classifyValidationErrors([
      "active: usage.refresh_secs must be an integer >= 30",
      "active: usage.refresh_secs must be an integer >= 30",
      "active: hardware.brightness must be an integer from 0 to 100",
    ]);
    expect(messagesForSection(issues, "usage")).toEqual({
      refresh_secs: ["active: usage.refresh_secs must be an integer >= 30"],
    });
  });

  it("opens Advanced ancestors and focuses the invalid field", () => {
    const root = document.createElement("section");
    root.innerHTML = '<details><label class="field"><span data-config-key="web_port"></span><input /></label></details>';
    document.body.appendChild(root);
    const field = root.querySelector<HTMLElement>(".field")!;
    field.scrollIntoView = vi.fn();

    expect(revealValidationField(root, "web_port")).toBe(true);
    expect(root.querySelector("details")?.open).toBe(true);
    expect(document.activeElement).toBe(root.querySelector("input"));
    expect(field.scrollIntoView).toHaveBeenCalled();
    root.remove();
  });
});
