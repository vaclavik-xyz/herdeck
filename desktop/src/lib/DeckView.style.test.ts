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
    .map(([, selector, body]) => ({
      selectors: selector.split(",").map((s) => s.trim()),
      body,
    }));
}

function atRuleBody(css: string, prelude: string): string | null {
  const start = css.indexOf(prelude);
  if (start < 0) return null;
  const match = css.slice(start).match(/\{((?:[^{}]|\{[^{}]*\})*)\}/);
  return match ? match[1] : null;
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
const TARGETS = [".cell.active", ".panel.active"];

describe("press flash styling", () => {
  const rules = topLevelRules(STYLE);
  const declaring = (selector: string, pattern: RegExp) =>
    rules.filter((r) => r.selectors.includes(selector) && pattern.test(r.body));

  it.each(TARGETS)("%s flashes on a press", (target) => {
    expect(
      declaring(target, /animation:\s*press-a/).length,
      `nothing gives ${target} the press animation`,
    ).toBeGreaterThan(0);
  });

  it.each(TARGETS)("%s restarts its flash on a repeat press", (target) => {
    expect(
      declaring(`${target}.alt`, /animation-name:\s*press-b/).length,
      `nothing swaps the animation name for ${target}.alt, so a re-press looks dropped`,
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
    const reduced = atRuleBody(STYLE, "@media (prefers-reduced-motion: reduce)");

    it("has a rule at all", () => {
      expect(reduced, "no reduced-motion rule for the press parity").not.toBeNull();
    });

    it.each(TARGETS)("%s still shows a repeat press", (target) => {
      const parity = topLevelRules(reduced ?? "")
        .find((r) => r.selectors.includes(`${target}.alt`));
      expect(parity, `reduced motion leaves ${target}.alt with no visual difference`).toBeDefined();

      const plain = ringColour(
        rules.find((r) => r.selectors.includes(target))?.body ?? "",
      );
      const alternate = ringColour(parity!.body);
      expect(plain, `${target} paints no ring colour to differ from`).toBeTruthy();
      expect(alternate, `${target}.alt paints no ring colour`).toBeTruthy();
      expect(alternate, `both presses paint ${target} the same, so they look identical`)
        .not.toBe(plain);
    });
  });
});
