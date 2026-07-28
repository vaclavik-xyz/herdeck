// @vitest-environment node
// Enforcement for two defect classes that only show up on a real device, which
// is exactly where nobody looks: a field whose fixed label track never collapses
// (one such field put the whole Connections page into a horizontal scroll on a
// phone), and a component that leaves its own appearance to ambient CSS.
import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, it, expect } from "vitest";

const FIELDS_DIR = fileURLToPath(new URL("./fields", import.meta.url));
const MOBILE_BREAKPOINT = "@media (max-width: 760px)";

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");
}

// Comments are stripped: the rules are what ships, and a comment that merely
// NAMES a banned construct must not read as a use of it.
function styleBlock(source: string): string {
  const match = source.match(/<style>([\s\S]*)<\/style>/);
  return match ? match[1].replace(/\/\*[\s\S]*?\*\//g, "") : "";
}

describe("responsive field layout", () => {
  const fields = readdirSync(FIELDS_DIR).filter((f) => f.endsWith(".svelte"));

  // The two-column field grid is `var(--field-label-w) minmax(0, 1fr)`, and
  // --field-label-w is a FIXED 240px. On a 390px phone that leaves ~118px for
  // the control, and any label wider than the track pushes the form past the
  // viewport. Every consumer must therefore collapse to one column.
  const labelled = fields.filter((f) => read(`./fields/${f}`).includes("var(--field-label-w"));

  it("has consumers of the fixed label track to check", () => {
    expect(labelled.length).toBeGreaterThan(4);
  });

  // Asserting the media query merely EXISTS would pass on a block that only
  // tweaks a font size; assert the track itself is redeclared without the
  // fixed label column, which is the defect.
  it.each(labelled)("%s collapses its label track on a phone", (file) => {
    const css = styleBlock(read(`./fields/${file}`));
    expect(css).toContain(MOBILE_BREAKPOINT);
    const mobile = css.slice(css.indexOf(MOBILE_BREAKPOINT));
    const redeclared = [...mobile.matchAll(/grid-template-columns:\s*([^;]+);/g)].map((m) => m[1]);
    expect(redeclared.length, "the mobile block never redeclares the grid").toBeGreaterThan(0);
    for (const value of redeclared) {
      expect(value, "the phone layout still carries the fixed label track")
        .not.toContain("var(--field-label-w");
    }
  });

  // FieldCopy pins its parts to explicit grid coordinates that assume the
  // desktop two-column form; the error line must not keep column 2 once its
  // consumers collapse, or it silently creates an implicit second track.
  it("moves the validation message out of column 2 on a phone", () => {
    const css = styleBlock(read("./fields/FieldCopy.svelte"));
    const mobile = css.slice(css.indexOf(MOBILE_BREAKPOINT));
    expect(mobile).toMatch(/\.fielderror\s*\{[^}]*grid-column:\s*1/);
  });
});

describe("ConfirmRemoveButton owns its appearance", () => {
  // It renders inside a <legend> in six sections. Svelte scoped styles never
  // cross a component boundary, so a parent's `button {…}` rule cannot reach
  // it: anything it does not declare itself falls back to the UA's light-grey
  // chrome button, on a dark panel.
  const css = styleBlock(read("./fields/ConfirmRemoveButton.svelte"));
  // Every BARE `button {…}` rule, not just the first: the surface may be split
  // or reordered, and taking only match [0] would silently retarget.
  const base = [...css.matchAll(/(?:^|\n)\s*button\s*\{([^}]*)\}/g)].map((m) => m[1]).join("\n");

  it("declares its own surface rather than inheriting one", () => {
    expect(base).toMatch(/background:/);
    expect(base).toMatch(/border:/);
    expect(base).toMatch(/border-radius:/);
  });

  it("needs no !important, because it overrides nothing", () => {
    expect(css).not.toContain("!important");
  });
});
