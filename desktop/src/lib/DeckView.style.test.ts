// @vitest-environment node
// The press retrigger has two halves: the class churn (asserted by mounting, in
// DeckView.press.test.ts) and the CSS that makes it visible. jsdom applies no
// scoped Svelte styles, so this half is asserted from source — otherwise the
// rules could be deleted with every test still green and 13 nodes churning a
// class for nothing.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, it, expect } from "vitest";

const COMMENT = /\/\*[\s\S]*?\*\//g;
// Block-less at-rules first: without this the block form's prelude would run
// from an `@import …;` to the last `{` in the sheet and swallow everything.
const AT_STATEMENT = /@[a-z-]+[^;{}]*;/g;
// One level of nesting: enough for @media { rule {} } and @keyframes { from {} }.
const AT_RULE = /@[a-z-]+[^{};]*\{(?:[^{}]|\{[^{}]*\})*\}/g;

const SOURCE = readFileSync(fileURLToPath(new URL("./DeckView.svelte", import.meta.url)), "utf8");
// Comments are stripped ONCE, here: every matcher below locates blocks by text,
// so a comment that merely mentions a selector or an at-rule would otherwise
// send one of them off to parse the wrong region.
const STYLE = (SOURCE.match(/<style>([\s\S]*)<\/style>/)?.[1] ?? "").replace(COMMENT, "");

/** Rules outside any at-rule. The parser is brace-naive and takes a selector to
 *  be "everything since the last `}`", so at-rule blocks must go first —
 *  otherwise their inner rules flatten into the same list and the lookups
 *  silently depend on where in the file things happen to sit. */
function topLevelRules(css: string): { selectors: string[]; body: string }[] {
  return [...css.replace(AT_STATEMENT, "").replace(AT_RULE, "").matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .map(([, selector, body]) => ({ selectors: splitSelectorList(selector), body }));
}

/** Split a selector list on commas that separate SELECTORS, not the ones inside
 *  `:not(.a, .b)` — a fragment cut mid-argument keeps its opening paren, and the
 *  strip in trailingClasses would then read the excluded class as required. */
function splitSelectorList(list: string): string[] {
  const groups: string[] = [];
  const masked = list.replace(/\([^()]*\)/g, (group) => `\u0000${groups.push(group) - 1}\u0000`);
  return masked
    .split(",")
    .map((part) => part.replace(/\u0000(\d+)\u0000/g, (_, i) => groups[Number(i)]).trim());
}

function atRuleBody(css: string, prelude: RegExp): string | null {
  const at = css.match(prelude);
  if (at?.index == null) return null;
  const match = css.slice(at.index).match(/\{((?:[^{}]|\{[^{}]*\})*)\}/);
  return match ? match[1] : null;
}

/** The classes of a selector's LAST compound: ".deck .cell.active" -> {cell,
 *  active}. Comparing class sets rather than selector text keeps a scoping
 *  prefix, a reordered compound or `:is()` from reading as a deleted rule. */
function trailingClasses(selector: string): Set<string> {
  // Strip BEFORE splitting on combinators: `:not(.alt, .fade)` contains a space,
  // so a descendant split would otherwise cut the compound mid-argument.
  // A functional pseudo's argument states a CONDITION — classes this compound
  // must lack (`:not(.alt)`) or that some other element must have (`:has(.alt)`)
  // — never classes this element itself carries. Collecting them would read
  // `:not(.alt)` as requiring alt, which both rejects a legal base rule and
  // accepts it as the parity rule it is the exact opposite of.
  const last = selector
    .replace(/:(?:not|has)\([^)]*\)/g, "")
    .trim()
    .split(/[\s>+~]+/)
    .pop() ?? "";
  return new Set([...last.matchAll(/\.([A-Za-z0-9_-]+)/g)].map((m) => m[1]));
}

/** Is the rule scoped UNDER an element carrying all of `classes`? Unlike
 *  `reaches`, which asks what a rule targets, this asks what it is about:
 *  `.deck-offline.mini strong` styles the mini overlay even though it targets a
 *  bare `strong`, which carries no classes to match on. */
function scopedUnder(selector: string, classes: string[]): boolean {
  return selector
    .replace(/:(?:not|has)\([^)]*\)/g, "")
    .trim()
    .split(/[\s>+~]+/)
    .some((compound) => {
      const have = new Set([...compound.matchAll(/\.([A-Za-z0-9_-]+)/g)].map((m) => m[1]));
      return classes.every((c) => have.has(c));
    });
}

/** A selector's last compound: ".panel img" -> "img". Pairs with `scopedUnder`
 *  to name a bare tag inside a classed ancestor, which `trailingClasses` cannot
 *  distinguish because such a compound carries no classes at all. */
function lastCompound(selector: string): string {
  return (
    selector
      .replace(/:(?:not|has)\([^)]*\)/g, "")
      .trim()
      .split(/[\s>+~]+/)
      .pop() ?? ""
  );
}

/** Does any selector in the list reach an element with all of `classes` (and
 *  none of `without`)? */
function reaches(selectors: string[], classes: string[], without: string[] = []): boolean {
  return selectors.some((selector) => {
    const have = trailingClasses(selector);
    return classes.every((c) => have.has(c)) && without.every((c) => !have.has(c));
  });
}

/** The colour a rule paints the ring with, from `outline` or `outline-color`. */
function ringColour(body: string): string | undefined {
  return (
    body.match(/outline-color:\s*([^;]+)/)?.[1]
    ?? body.match(/outline:[^;]*?(var\(--[a-z-]+\)|#[0-9a-fA-F]{3,8}|rgba?\([^)]*\))/)?.[1]
  )?.trim();
}

// The panel is a press target in its own right (`press(view.slots)`), so every
// rule that carries the flash must reach it too. Asserted per target rather than
// per selector LIST, so splitting the shared rule stays legal.
const TARGETS: [string, string[]][] = [
  [".cell.active", ["cell", "active"]],
  [".panel.active", ["panel", "active"]],
];

describe("press flash styling", () => {
  const rules = topLevelRules(STYLE);
  const declaring = (classes: string[], pattern: RegExp, without: string[] = []) =>
    rules.filter((r) => reaches(r.selectors, classes, without) && pattern.test(r.body));

  // `without: ["alt"]` is load-bearing: a class-set match is a SUPERSET match,
  // so without it a sheet whose only press-a rule is the `.alt` one would pass
  // while the first press — parity false, no `.alt` — flashed nothing at all.
  it.each(TARGETS)("%s flashes on a press", (label, classes) => {
    expect(
      declaring(classes, /animation:\s*press-a/, ["alt"]).length,
      `nothing gives ${label} the press animation`,
    ).toBeGreaterThan(0);
  });

  it.each(TARGETS)("%s restarts its flash on a repeat press", (label, classes) => {
    expect(
      declaring([...classes, "alt"], /animation-name:\s*press-b/).length,
      `nothing swaps the animation name for ${label}.alt, so a re-press looks dropped`,
    ).toBeGreaterThan(0);
  });

  it("keeps the two keyframes identical, or alternate presses would differ", () => {
    const bodies = [...STYLE.matchAll(/@keyframes press-[ab]\s*\{((?:[^{}]|\{[^{}]*\})*)\}/g)]
      .map((m) => m[1].replace(/\s+/g, " ").trim());
    expect(bodies.length, "expected exactly the two parity keyframes").toBe(2);
    expect(bodies[0]).toBe(bodies[1]);
  });

  // theme.css flattens animation-duration to .01ms !important under reduced
  // motion, so the parity needs a difference that is not an animation at all —
  // and one that actually LOOKS different from the ring a plain press shows.
  describe("under reduced motion", () => {
    const reduced = atRuleBody(
      STYLE,
      /@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)/,
    );

    it("has a rule at all", () => {
      expect(reduced, "no reduced-motion rule for the press parity").not.toBeNull();
    });

    it.each(TARGETS)("%s still shows a repeat press", (label, classes) => {
      const parity = topLevelRules(reduced ?? "")
        .find((r) => reaches(r.selectors, [...classes, "alt"]));
      expect(parity, `reduced motion leaves ${label}.alt with no visual difference`).toBeDefined();

      // Last wins: with the ring split across rules the cascade takes the later
      // declaration, so the test must compare what the device would actually show.
      const plain = rules
        .filter((r) => reaches(r.selectors, classes, ["alt"]))
        .map((r) => ringColour(r.body))
        .filter((c): c is string => c != null)
        .at(-1);
      const alternate = ringColour(parity!.body);
      expect(plain, `${label} paints no ring colour to differ from`).toBeTruthy();
      expect(alternate, `${label}.alt paints no ring colour`).toBeTruthy();
      expect(alternate, `both presses paint ${label} the same, so they look identical`)
        .not.toBe(plain);
    });
  });
});

// The compact overlay is a `mini` MODIFIER: mounting proves the class is there,
// but everything the modifier means lives in CSS. Deleted, the floating deck
// would render the desktop card — --s5 padding, --t-h2 heading — inside a 328px
// window, with every mounted test still green.
describe("compact offline pill styling", () => {
  const rules = topLevelRules(STYLE);
  // Two scopes, asserted apart. Lumping them together lets a PARTIAL deletion
  // pass: the pill's own `padding: 4px 10px` would satisfy a padding check while
  // the container fell back to the card's --s5, and pointer-events moved onto
  // the pill would leave the inset:0 container swallowing every press — the very
  // bug this guard exists for.
  const container = rules.filter((r) => reaches(r.selectors, ["deck-offline", "mini"]));
  const pill = rules.filter((r) =>
    r.selectors.some((s) => scopedUnder(s, ["deck-offline", "mini"]) && trailingClasses(s).size === 0),
  );
  const declares = (scope: typeof rules, pattern: RegExp) =>
    scope.some((r) => pattern.test(r.body));

  it("styles the mini variant at all", () => {
    expect(container.length, "nothing distinguishes the compact overlay from the desktop card")
      .toBeGreaterThan(0);
    expect(pill.length, "nothing shrinks the heading into a pill").toBeGreaterThan(0);
  });

  it.each([
    // The whitespace lives INSIDE the lookahead: with `\s*` in front of it the
    // engine backtracks that to empty, the lookahead then reads a space rather
    // than `var(`, succeeds, and the rejection rejects nothing.
    ["padding of its own, not the card's --s5", /padding:(?![^;]*var\(--s5\))/],
    ["a scrim over the dimmed keys", /background:/],
  ])("gives the compact overlay %s", (_what, pattern) => {
    expect(declares(container, pattern)).toBe(true);
  });

  it.each([
    ["the pill outline", /border-radius:/],
    ["a label-sized font, not --t-h2", /font:\s*var\(--t-label\)/],
  ])("gives the compact heading %s", (_what, pattern) => {
    expect(declares(pill, pattern)).toBe(true);
  });

  // A single failed poll flips `online` false, so this overlay appears over a
  // deck that still actuates. Without this the floating deck goes unclickable
  // for ~300ms at a time — and only for the mouse, since keys 1-9 bypass it.
  // It must sit on the CONTAINER: that is the inset:0 element over the keys.
  it("lets presses through to the keys underneath", () => {
    expect(
      declares(container, /pointer-events:\s*none/),
      "the compact overlay swallows presses the deck would still have accepted",
    ).toBe(true);
  });
});

// The row height must come from the square tiles, never from the panel. The
// panel spans `grid-column: 4 / 6` — two columns PLUS the gap between them — so
// its 2:1 artwork (392x196, against 196x196 tiles) wants (2C + gap) / 2 of
// height, i.e. gap/2 MORE than the tiles. In an auto-height grid row the img's
// `height: 100%` degenerates to auto, so it sizes itself, drags the row with
// it, and the panel hangs below the tiles beside it. Only an out-of-flow image
// hands the row height back to the tiles, and jsdom cannot see any of this.
describe("status panel row geometry", () => {
  const rules = topLevelRules(STYLE);
  const panelImg = rules.filter((r) =>
    r.selectors.some((s) => scopedUnder(s, ["panel"]) && lastCompound(s) === "img"),
  );
  const panelBox = rules.filter((r) => reaches(r.selectors, ["panel"], ["active"]));
  const declares = (scope: typeof rules, pattern: RegExp) => scope.some((r) => pattern.test(r.body));

  it("takes the panel image out of flow so it cannot size the row", () => {
    expect(panelImg.length, "no rule reaches the panel's image").toBeGreaterThan(0);
    expect(
      declares(panelImg, /position:\s*absolute/),
      "the panel image is in flow, so its 2:1 ratio sets the row height and the panel overhangs the tiles",
    ).toBe(true);
  });

  // Without this the image escapes to the nearest positioned ancestor — the
  // deck card — and lands somewhere else entirely.
  it("makes the panel itself the containing block", () => {
    expect(
      declares(panelBox, /position:\s*relative/),
      "the panel is not positioned, so its absolute image is placed against the wrong box",
    ).toBe(true);
  });

  // The panel box is gap-width wider than two tiles. Filling it would stretch
  // the 2:1 art horizontally — the same distortion that forced the D200's
  // native small-window fix.
  it("letterboxes rather than stretches the artwork", () => {
    expect(
      declares(panelImg, /object-fit:\s*contain/),
      "the panel image is stretched across the extra gap width instead of being letterboxed",
    ).toBe(true);
  });
});

describe("compact deck chrome", () => {
  // The compact offline pill exists BECAUSE this footer is invisible there. If
  // it ever stops being clipped, the pill repeats text the user can already
  // read — and DeckView.offline.test.ts states this premise without being able
  // to observe it, since jsdom applies no scoped styles.
  const hidden = topLevelRules(STYLE).filter((r) =>
    r.selectors.some((s) => scopedUnder(s, ["deck", "compact"]) && reaches([s], ["summary"])),
  );

  it("keeps the compact status footer sr-only", () => {
    expect(hidden.length, "no rule hides the status footer on the compact deck").toBeGreaterThan(0);
    expect(
      // Either clipping property: `clip-path: inset(50%)` upholds the premise
      // just as well, and this guard is about the premise, not the spelling.
      hidden.some((r) => /clip(-path)?:/.test(r.body) && /width:\s*1px/.test(r.body)),
      "the compact footer is no longer clipped, so the offline pill now duplicates it",
    ).toBe(true);
  });
});
