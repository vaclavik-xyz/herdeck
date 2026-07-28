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

/** Rules OUTSIDE any @media — what a desktop viewport actually gets.
 *  MEDIA_BLOCK tolerates exactly one level of nesting, whatever the inner block
 *  is: anything TWO levels deep — a nested selector, or an at-rule carrying its
 *  own rules — leaves the block unstripped and hands its rules over as
 *  unconditional, failing OPEN. That is how a guard quietly stops guarding, so
 *  the stripper asserts its own post-condition instead of trusting the result. */
const CONDITIONAL_AT_RULE = /@(media|supports|container)\b/;

function unconditional(css: string): string {
  const out = css.replace(MEDIA_BLOCK, "");
  // Only the at-rules that can HIDE a rule from a desktop viewport matter here;
  // @keyframes, @font-face and an "@" inside a url() are none of this guard's
  // business and must not send the next author after the regex.
  const leftover = out.match(CONDITIONAL_AT_RULE);
  if (leftover) {
    throw new Error(
      `${leftover[0]} survived stripping — rules inside it would be read as `
      + "unconditional. This stripper handles @media nested one level deep and "
      + "nothing else; teach it the construct rather than trusting the result",
    );
  }
  return out;
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

// Both guards below read the stylesheet through unconditional(); if it ever
// under-strips they go vacuous with every test still green, so it gets its own.
describe("the media stripper", () => {
  it("keeps base rules and drops everything a media query holds", () => {
    const out = unconditional(
      ".field { color: red; }\n@media (max-width: 760px) {\n  .field { color: blue; }\n  .rows { gap: 0; }\n}\n.after { color: green; }",
    );
    expect(rules(out).map(([selector]) => selector)).toEqual([".field", ".after"]);
    expect(out).not.toContain("blue");
  });

  // One case per alternative in CONDITIONAL_AT_RULE, each asserting WHICH
  // at-rule leaked: a fixture containing two of them would otherwise keep
  // throwing after its own alternative was deleted from the pattern.
  it.each([
    // Depth, not at-rule-ness, is what defeats MEDIA_BLOCK: it tolerates one
    // level of nesting, and @keyframes puts its rules two levels down. @media
    // is the only conditional in the fixture, so this pins `media` itself.
    ["@media", "a media block it cannot parse",
      "@media (max-width: 760px) { @keyframes spin { from { opacity: 0 } } }"],
    ["@supports", "a top-level @supports",
      "@supports (display: grid) { .field { color: blue; } }"],
    ["@container", "a top-level @container",
      "@container (min-width: 400px) { .field { color: blue; } }"],
  ])("refuses to guess at %s in %s", (at, _name, css) => {
    expect(() => unconditional(css)).toThrow(new RegExp(`^${at} survived stripping`));
  });

  // The guard keys on at-rules that can HIDE a rule from a desktop viewport.
  // Without this case, widening it back to a bare "@" check — which trips on a
  // spinner or a retina asset — would leave the whole suite green.
  it("ignores at-rules and stray @ that hide nothing", () => {
    expect(() =>
      unconditional(
        '.icon { background: url("icon@2x.png"); content: "@"; }\n'
        + "@keyframes spin { from { opacity: 0 } to { opacity: 1 } }\n"
        + '@font-face { font-family: x; src: url("x@2.woff2"); }',
      ),
    ).not.toThrow();
  });
});

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
      // The SHAPE, not the absence of the token: --field-label-w is literally
      // 240px, so `240px minmax(0, 1fr)` would dodge a token-name check while
      // reproducing the horizontal scroll exactly. minmax(0, 1fr) specifically,
      // not a bare 1fr — 1fr floors at min-content, which overflows just as far.
      expect(
        value.trim(),
        `${selector} must collapse to a minmax(0, 1fr) first track on a phone`,
      ).toMatch(/^minmax\(0,\s*1fr\)/);
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
  // the phone block would leave the desktop button as UA chrome. Resolved
  // lazily so an unparsable stylesheet fails a test, not collection.
  const base = (): string =>
    rules(unconditional(css))
      .filter(([selector]) => selector === "button")
      .map(([, body]) => body)
      .join("\n");

  it("declares its own surface rather than inheriting one", () => {
    const surface = base();
    expect(surface).toMatch(/background:/);
    expect(surface).toMatch(/border:/);
    expect(surface).toMatch(/border-radius:/);
  });

  it("needs no !important, because it overrides nothing", () => {
    expect(css).not.toContain("!important");
  });
});
