import { describe, expect, it, vi } from "vitest";
import { parseConfig } from "./configClient";
import {
  classifyValidationErrors,
  classifyValidationIssue,
  fieldValidationKey,
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
    const inheritedBase = parseConfig({
      base: { deck: { overview_order: ["ghost"] } },
      profiles: { work: {} },
      active_profile: "work",
    })!;
    expect(classifyValidationIssue("active: unknown server 'ghost'", inheritedBase)).toMatchObject({
      section: "deck", fieldKey: "overview_order", owner: null, profileContext: null,
    });

    const inheritedProfile = parseConfig({
      profiles: { parent: { servers: ["ghost"] }, work: { extends: "parent" } },
      active_profile: "work",
    })!;
    expect(classifyValidationIssue("work: unknown server 'ghost'", inheritedProfile)).toMatchObject({
      section: "profiles", fieldKey: "servers", owner: "parent", profileContext: null,
    });
  });

  it("retains the non-active profile whose overlay needs editing", () => {
    expect(classifyValidationIssue("night: usage.refresh_secs must be an integer >= 30")).toMatchObject({
      section: "usage", fieldKey: "refresh_secs", profileContext: "night",
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

  it("focuses only the matching repeated owner", () => {
    const root = document.createElement("section");
    root.innerHTML = ["one", "two"].map((owner) =>
      `<label class="field"><span data-config-key="token_env" data-config-owner="${owner}"></span><input data-owner="${owner}" /></label>`,
    ).join("");
    document.body.appendChild(root);
    for (const field of root.querySelectorAll<HTMLElement>(".field")) field.scrollIntoView = vi.fn();

    expect(revealValidationField(root, "token_env", "two")).toBe(true);
    expect((document.activeElement as HTMLElement).dataset.owner).toBe("two");
    expect(fieldValidationKey("token_env", "two")).not.toBe(fieldValidationKey("token_env", "one"));
    root.remove();
  });
});
