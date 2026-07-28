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

  it.each(labelled)("%s collapses its label track on a phone", (file) => {
    expect(styleBlock(read(`./fields/${file}`))).toContain(MOBILE_BREAKPOINT);
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
  const base = css.match(/(?:^|\n)\s*button\s*\{([^}]*)\}/)?.[1] ?? "";

  it("declares its own surface rather than inheriting one", () => {
    expect(base).toMatch(/background:/);
    expect(base).toMatch(/border:/);
    expect(base).toMatch(/border-radius:/);
  });

  it("needs no !important, because it overrides nothing", () => {
    expect(css).not.toContain("!important");
  });
});
