// @vitest-environment node
// Enforcement: the window's status colours ARE the deck's status colours.
// PALETTE mirrors src/herdeck/driver/base.py COLORS; theme.css must mirror
// PALETTE literally, so a backend palette change cannot silently leave the
// window disagreeing with the hardware it drives.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, it, expect } from "vitest";
import { PALETTE } from "./statusColors";

const THEME = readFileSync(fileURLToPath(new URL("./theme.css", import.meta.url)), "utf8");

function tokens(css: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [, name, value] of css.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)) {
    out[name] = value.trim();
  }
  return out;
}

// Normalizes a #rrggbb or rgb()/rgba() colour to a comparable "r,g,b" triple,
// so a token written in a different notation from its PALETTE counterpart
// still gets caught (e.g. a hex brand colour that happens to equal a PALETTE
// rgb() value).
function toRgbTriple(value: string): string | null {
  const hex = value.trim().match(/^#([0-9a-fA-F]{6})$/);
  if (hex) {
    const n = hex[1];
    return [n.slice(0, 2), n.slice(2, 4), n.slice(4, 6)].map((h) => parseInt(h, 16)).join(",");
  }
  const rgb = value.trim().match(/^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/);
  if (rgb) return [rgb[1], rgb[2], rgb[3]].map(Number).join(",");
  return null;
}

describe("theme tokens", () => {
  const T = tokens(THEME);

  it("defines a --st-<name> token for every palette entry, with the same value", () => {
    for (const [name, value] of Object.entries(PALETTE)) {
      expect(T[`--st-${name}`], `missing --st-${name}`).toBeDefined();
      expect(T[`--st-${name}`].replace(/\s+/g, "")).toBe(value.replace(/\s+/g, ""));
    }
  });

  it("resolves every semantic status alias to a defined palette token", () => {
    const aliases = ["working", "idle", "blocked", "done", "waiting", "offline", "unknown"];
    for (const alias of aliases) {
      const raw = T[`--st-${alias}`];
      expect(raw, `missing --st-${alias}`).toBeDefined();
      const ref = raw.match(/^var\((--st-[a-z]+)\)$/)?.[1];
      expect(ref, `--st-${alias} must reference a palette token, got "${raw}"`).toBeDefined();
      expect(T[ref as string], `--st-${alias} points at undefined ${ref}`).toBeDefined();
    }
  });

  it("defines the layout, type, spacing and motion tokens the surfaces rely on", () => {
    for (const name of [
      "--canvas", "--panel", "--panel-raised", "--key", "--field", "--line", "--line-strong",
      "--text", "--text-dim", "--text-faint",
      "--accent", "--accent-strong", "--accent-soft", "--accent-ring",
      "--font-ui", "--font-mono",
      "--t-display", "--t-h2", "--t-body", "--t-label", "--t-help", "--t-eyebrow", "--t-mono",
      "--s1", "--s2", "--s3", "--s4", "--s5", "--s6", "--s8", "--s10",
      "--r-control", "--r-panel", "--r-stage", "--dur", "--ease",
      "--measure-form", "--measure-wide", "--field-label-w",
      "--control-sm", "--control-md", "--control-lg", "--control-xl",
    ]) {
      expect(T[name], `missing ${name}`).toBeDefined();
    }
  });

  it("keeps brand and form-affordance accents visually distinct from every status palette value", () => {
    // A user can assign any PALETTE colour to a live status (ThemeSection), so
    // a --brand-*/--form-* accent must never render identically to one — that
    // would make a hardcoded brand mark or keychain affordance indistinguishable
    // from a user-assigned agent status.
    const paletteTriples = new Set(
      Object.values(PALETTE)
        .map((v) => toRgbTriple(v))
        .filter((v): v is string => v !== null),
    );
    const guarded = Object.keys(T).filter((name) => name.startsWith("--brand-") || name.startsWith("--form-"));
    expect(guarded.length, "expected at least one --brand-*/--form-* token to guard").toBeGreaterThan(0);
    for (const name of guarded) {
      const triple = toRgbTriple(T[name]);
      expect(triple, `${name} value "${T[name]}" is not a parseable hex/rgb colour`).not.toBeNull();
      expect(paletteTriples.has(triple as string), `${name} (${T[name]}) collides with a PALETTE value`).toBe(false);
    }
  });
});

// The refactor's ratchet: every file listed here has been converted to tokens
// and must stay free of colour literals. Paths are relative to src/. The list
// grows task by task; the final task asserts it covers every .svelte file.
const CONVERTED = [
  "lib/fields/FieldCopy.svelte",
  "lib/fields/TextField.svelte",
  "lib/fields/SelectField.svelte",
  "lib/fields/NumberField.svelte",
  "lib/fields/BooleanField.svelte",
  "lib/fields/ListField.svelte",
  "lib/fields/OverrideField.svelte",
  "lib/fields/TriStateListField.svelte",
  "lib/fields/TokenSecretField.svelte",
  "lib/fields/ProviderPicker.svelte",
  "lib/fields/ConfirmRemoveButton.svelte",
  "lib/fields/ColorSwatchField.svelte",
  "lib/sections/FieldGroup.svelte",
  "lib/sections/DeckSection.svelte",
  "lib/sections/ViewSection.svelte",
  "lib/sections/ThemeSection.svelte",
  "lib/sections/ServersSection.svelte",
  "lib/sections/MacrosSection.svelte",
  "lib/sections/StartProfilesSection.svelte",
  "lib/sections/AnswerProfilesSection.svelte",
  "lib/sections/NotificationsSection.svelte",
  "lib/sections/SafetySection.svelte",
  "lib/sections/UsageSection.svelte",
  "lib/sections/ProfilesSection.svelte",
  "lib/sections/DesktopSection.svelte",
  "lib/StatusRibbon.svelte",
  "lib/DeckView.svelte",
  "lib/Banner.svelte",
  "ConfigApp.svelte",
  "App.svelte",
  "lib/Onboarding.svelte",
];

describe("token discipline", () => {
  it.each(CONVERTED)("%s carries no colour literals", (rel) => {
    const src = readFileSync(fileURLToPath(new URL(`../${rel}`, import.meta.url)), "utf8");
    const literals = [
      ...src.matchAll(/#[0-9a-fA-F]{3,8}\b/g),
      ...src.matchAll(/\brgba?\(/g),
    ].map((m) => m[0]);
    expect(literals, `use var(--token) instead of ${literals.join(", ")}`).toEqual([]);
  });
});
