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

const MEDIA_BLOCK = /@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}/g;

/** Rules OUTSIDE any @media — what a desktop viewport actually gets. */
function unconditional(css: string): string {
  return css.replace(MEDIA_BLOCK, "");
}

/** The body of the phone media block, or "" when there is none. */
function phoneBlock(css: string): string {
  for (const block of css.match(MEDIA_BLOCK) ?? []) {
    if (block.startsWith(MOBILE_BREAKPOINT)) return block;
  }
  return "";
}

/** [selector, body] for each rule in a stylesheet fragment (one level deep). */
function rules(css: string): [string, string][] {
  return [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .map(([, selector, body]) => [selector.trim(), body] as [string, string])
    .filter(([selector]) => !selector.startsWith("@"));
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
  // tweaks a font size, and asserting that SOME rule in it redeclares the grid
  // would pass on a nested one (.rows, .setrow) while the root field kept its
  // fixed track. Anchor to the root: the selector that declares the track.
  const TRACK = /grid-template-columns:\s*([^;}]+)/;

  it.each(labelled)("%s collapses its label track on a phone", (file) => {
    const css = styleBlock(read(`./fields/${file}`));
    const root = rules(unconditional(css)).find(
      ([, body]) => body.includes("var(--field-label-w") && TRACK.test(body),
    );
    expect(root, "no rule declares the fixed label track — update this guard").toBeDefined();

    const [selector] = root!;
    const collapsed = rules(phoneBlock(css))
      .filter(([sel]) => sel === selector)
      .map(([, body]) => body.match(TRACK)?.[1])
      .filter((v): v is string => v != null);

    expect(collapsed.length, `${selector} never redeclares its grid on a phone`)
      .toBeGreaterThan(0);
    for (const value of collapsed) {
      expect(value, `${selector} still carries the fixed label track on a phone`)
        .not.toContain("var(--field-label-w");
    }
  });

  // FieldCopy pins its parts to explicit grid coordinates that assume the
  // desktop two-column form; the error line must not keep column 2 once its
  // consumers collapse, or it silently creates an implicit second track.
  it("gives the validation message a whole row of its own on a phone", () => {
    const body = rules(phoneBlock(styleBlock(read("./fields/FieldCopy.svelte"))))
      .find(([sel]) => sel === ".fielderror")?.[1];
    expect(body, "FieldCopy no longer repositions its message on a phone").toBeDefined();
    // 1 / -1, not 1: TokenSecretField's phone grid keeps a second column, and a
    // message occupying only column 1 lets the key badge sit beside it while
    // its own button drops to the next row.
    expect(body).toMatch(/grid-column:\s*1\s*\/\s*-1/);
  });
});

describe("ConfirmRemoveButton owns its appearance", () => {
  // It renders inside a <legend> in six sections. Svelte scoped styles never
  // cross a component boundary, so a parent's `button {…}` rule cannot reach
  // it: anything it does not declare itself falls back to the UA's light-grey
  // chrome button, on a dark panel.
  const css = styleBlock(read("./fields/ConfirmRemoveButton.svelte"));
  // Every BARE `button {…}` rule, not just the first (the surface may be split
  // or reordered), but only OUTSIDE a media query: a surface declared solely in
  // the phone block would leave the desktop button as UA chrome.
  const base = rules(unconditional(css))
    .filter(([selector]) => selector === "button")
    .map(([, body]) => body)
    .join("\n");

  it("declares its own surface rather than inheriting one", () => {
    expect(base).toMatch(/background:/);
    expect(base).toMatch(/border:/);
    expect(base).toMatch(/border-radius:/);
  });

  it("needs no !important, because it overrides nothing", () => {
    expect(css).not.toContain("!important");
  });
});
