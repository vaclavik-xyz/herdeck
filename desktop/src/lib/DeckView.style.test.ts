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

/** Hide every `(...)` behind a sentinel so a later split cannot cut inside one.
 *  Both splitters below need this: a fragment cut mid-argument keeps its opening
 *  paren, and the strip in `compounds` would then read an excluded class as
 *  required. One copy, so a grammar fix lands once. */
function maskGroups(text: string): { masked: string; unmask: (part: string) => string } {
  const groups: string[] = [];
  const masked = text.replace(/\([^()]*\)/g, (group) => `\u0000${groups.push(group) - 1}\u0000`);
  return {
    masked,
    unmask: (part) => part.replace(/\u0000(\d+)\u0000/g, (_, i) => groups[Number(i)]),
  };
}

/** Split a selector list on commas that separate SELECTORS, not the ones inside
 *  `:not(.a, .b)`. */
function splitSelectorList(list: string): string[] {
  const { masked, unmask } = maskGroups(list);
  return masked.split(",").map((part) => unmask(part).trim());
}

function atRuleBody(css: string, prelude: RegExp): string | null {
  const at = css.match(prelude);
  if (at?.index == null) return null;
  const match = css.slice(at.index).match(/\{((?:[^{}]|\{[^{}]*\})*)\}/);
  return match ? match[1] : null;
}

/** A selector's compounds with their functional pseudos INTACT: ".deck:not(.compact)
 *  .panel img" -> [".deck:not(.compact)", ".panel", "img"]. The single splitter;
 *  `compounds` is this plus a strip. Masking first is what lets the split be a
 *  plain combinator regex, since `:not(.alt, .fade)` contains a space. */
function rawCompounds(selector: string): string[] {
  const { masked, unmask } = maskGroups(selector);
  return masked
    .trim()
    .split(/[\s>+~]+/)
    .map(unmask);
}

/** A selector's compounds with functional pseudos REMOVED: ".deck .cell.active"
 *  -> [".deck", ".cell.active"]. Such an argument states a CONDITION — classes
 *  this compound must lack (`:not(.alt)`) or that some other element must have
 *  (`:has(.alt)`) — never classes this element itself carries. Collecting them
 *  would read `:not(.alt)` as REQUIRING alt, which both rejects a legal base
 *  rule and accepts it as the parity rule it is the exact opposite of. Callers
 *  that must HONOUR the condition use `rawCompounds` instead. */
function compounds(selector: string): string[] {
  return (
    rawCompounds(selector)
      .map((compound) => compound.replace(/:(?:not|has)\([^)]*\)/g, ""))
      // A compound that was ENTIRELY a pseudo (`.deck :has(.panel) .cell`) is
      // now an empty string rather than gone, and an empty compound satisfies
      // `scopedUnder`'s `every` vacuously. Dropping it restores what stripping
      // before the split used to do.
      .filter(Boolean)
  );
}

function classesOf(compound: string): Set<string> {
  return new Set([...compound.matchAll(/\.([A-Za-z0-9_-]+)/g)].map((m) => m[1]));
}

/** A selector's last compound: ".panel img" -> "img". The element a rule
 *  TARGETS, as opposed to the ancestors that scope it. */
function lastCompound(selector: string): string {
  return compounds(selector).pop() ?? "";
}

/** The classes of a selector's LAST compound: ".deck .cell.active" -> {cell,
 *  active}. Comparing class sets rather than selector text keeps a scoping
 *  prefix, a reordered compound or `:is()` from reading as a deleted rule. */
function trailingClasses(selector: string): Set<string> {
  return classesOf(lastCompound(selector));
}

/** Is the rule scoped UNDER an element carrying all of `classes`? Unlike
 *  `reaches`, which asks what a rule targets, this asks what it is about:
 *  `.deck-offline.mini strong` styles the mini overlay even though it targets a
 *  bare `strong`, which carries no classes to match on. */
function scopedUnder(selector: string, classes: string[]): boolean {
  return compounds(selector).some((compound) => {
    const have = classesOf(compound);
    return classes.every((c) => have.has(c));
  });
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

  // Filtering SELECTORS cannot answer this guard's question. Three separate
  // mutations proved it: scoping the declarations under `.deck.compact`,
  // scoping them away with `.deck:not(.compact)`, and — the one that defeats
  // any selector filter — leaving the base rule intact and overriding it with a
  // later, more specific `.deck.compact .panel img { position: static }`. What
  // the guard actually means is "each surface ENDS UP WITH these declarations",
  // so it computes the effective value per surface and subsumes all three.
  //
  // Last-match-wins stands in for the cascade. In this sheet compact overrides
  // are written after the base rules they override, so source order and
  // specificity agree; a compact rule placed BEFORE its base would fool this,
  // and that is the one shape it does not model.
  //
  // The panel's ancestor chain. The image's chain is this plus `panel` itself,
  // since the image sits INSIDE the panel — the distinction the first draft of
  // this matcher got wrong, which made every `.panel img` rule invisible and
  // read the panel's own `position` as the image's.
  const CHAIN = ["deck", "stage", "grid"];
  const SURFACES: [string, string[]][] = [
    ["the desktop card", CHAIN],
    ["the compact deck", [...CHAIN, "compact"]],
  ];

  /** Does one compound match an element carrying `classes` (and tag `tag`)?
   *  Required classes must be present, `:not()` classes must be absent, and a
   *  leading tag must be the element's. */
  function compoundMatches(compound: string, classes: Set<string>, tag?: string): boolean {
    const negated = new Set<string>();
    for (const not of compound.matchAll(/:not\(([^)]*)\)/g)) {
      for (const c of not[1].matchAll(/\.([A-Za-z0-9_-]+)/g)) negated.add(c[1]);
    }
    const bare = compound.replace(/:(?:not|has)\([^)]*\)/g, "");
    const named = bare.match(/^[a-zA-Z][a-zA-Z0-9-]*/)?.[0];
    if (named && named !== tag) return false;
    for (const c of classesOf(bare)) if (!classes.has(c)) return false;
    for (const c of negated) if (classes.has(c)) return false;
    return true;
  }

  /** Does `selector` reach the target element on a surface whose ancestor chain
   *  carries `surface`? The chain is treated as one class bag, which is exact
   *  here: every ancestor class in play sits on `.deck`, `.stage` or `.grid`. */
  function applies(
    selector: string,
    surface: Set<string>,
    target: Set<string>,
    tag?: string,
  ): boolean {
    const parts = rawCompounds(selector);
    const last = parts.pop();
    if (!last || !compoundMatches(last, target, tag)) return false;
    return parts.every((part) => compoundMatches(part, surface));
  }

  /** The value the surface actually gets for `prop`, or undefined if nothing
   *  declares it there. */
  function effective(
    prop: string,
    surface: Set<string>,
    target: Set<string>,
    tag?: string,
  ): string | undefined {
    // Global, and the LAST hit wins inside a rule as well as across them: a
    // block that declares the property twice (`position: absolute; …;
    // position: static;`) paints the second value, and a non-global match would
    // have read the first and called the guard satisfied.
    const pattern = new RegExp(`(?:^|;)\\s*${prop}:\\s*([^;]+)`, "g");
    let value: string | undefined;
    for (const rule of rules) {
      if (!rule.selectors.some((s) => applies(s, surface, target, tag))) continue;
      const hits = [...rule.body.matchAll(pattern)];
      if (hits.length) value = hits[hits.length - 1][1].trim();
    }
    return value;
  }

  // The image carries no classes of its own, so its target class set is empty
  // and only the tag names it.
  const onImage = (prop: string, chain: string[]) =>
    effective(prop, new Set([...chain, "panel"]), new Set(), "img");
  const onPanel = (prop: string, chain: string[]) =>
    effective(prop, new Set(chain), new Set(["panel"]));

  it.each(SURFACES)(
    "takes the panel image out of flow on %s, so it cannot size the row",
    (_name, chain) => {
      expect(
        onImage("position", chain),
        "the panel image is in flow, so its 2:1 ratio sets the row height and the panel overhangs the tiles",
      ).toBe("absolute");
    },
  );

  // Without this the image escapes to the nearest positioned ancestor —
  // `.stage`, which spans the whole grid plus its padding — and lands somewhere
  // else entirely. Someone debugging a stray full-grid image should look there,
  // not at `.deck`.
  it.each(SURFACES)("makes the panel itself the containing block on %s", (_name, chain) => {
    expect(
      onPanel("position", chain),
      "the panel is not positioned, so its absolute image is placed against .stage instead",
    ).toBe("relative");
  });

  // The panel box is gap-width wider than two tiles. Filling it would stretch
  // the 2:1 art horizontally — the same distortion that forced the D200's
  // native small-window fix.
  it.each(SURFACES)("letterboxes rather than stretches the artwork on %s", (_name, chain) => {
    expect(
      onImage("object-fit", chain),
      "the panel image is stretched across the extra gap width instead of being letterboxed",
    ).toBe("contain");
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
