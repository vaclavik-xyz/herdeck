import { describe, expect, it } from "vitest";
import { filterSettingsNavigation, type SettingsNavGroup } from "./settingsNavigation";

const groups: SettingsNavGroup[] = [
  { label: "Control", items: [{ key: "overview", icon: "⌂", label: "Overview" }] },
  {
    label: "Policies",
    items: [
      { key: "notifications", icon: "◌", label: "Notifications" },
      { key: "safety", icon: "◇", label: "Safety" },
    ],
  },
];

const descriptions = {
  overview: "Current runtime state",
  notifications: "Deliver alerts through Telegram",
  safety: "Confirm destructive actions",
};

describe("filterSettingsNavigation", () => {
  it("returns all groups for a blank query", () => {
    expect(filterSettingsNavigation(groups, descriptions, "  ", "en")).toBe(groups);
  });

  it("matches visible section names without case sensitivity", () => {
    expect(filterSettingsNavigation(groups, descriptions, "SAFE", "en")).toEqual([
      { label: "Policies", items: [{ key: "safety", icon: "◇", label: "Safety" }] },
    ]);
  });

  it("matches user-facing descriptions and removes empty groups", () => {
    expect(filterSettingsNavigation(groups, descriptions, "telegram", "en")).toEqual([
      { label: "Policies", items: [{ key: "notifications", icon: "◌", label: "Notifications" }] },
    ]);
  });
});
