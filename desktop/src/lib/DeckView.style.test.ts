// @vitest-environment node
// The press retrigger has two halves: the class churn (asserted by mounting, in
// DeckView.press.test.ts) and the CSS that makes it visible. jsdom applies no
// scoped Svelte styles, so this half is asserted from source — otherwise the
// rules could be deleted with every test still green and 13 nodes churning a
// class for nothing.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, it, expect } from "vitest";

const SOURCE = readFileSync(fileURLToPath(new URL("./DeckView.svelte", import.meta.url)), "utf8");
const STYLE = SOURCE.match(/<style>([\s\S]*)<\/style>/)?.[1] ?? "";

const COMMENT = /\/\*[\s\S]*?\*\//g;
// Block-less at-rules first: without this the block form's prelude would run
// from an `@import …;` to the last `{` in the sheet and swallow everything.
const AT_STATEMENT = /@[a-z-]+[^;{}]*;/g;
// One level of nesting: enough for @media { rule {} } and @keyframes { from {} }.
const AT_RULE = /@[a-z-]+[^{};]*\{(?:[^{}]|\{[^{}]*\})*\}/g;

/** Rules outside any at-rule. The parser is brace-naive and takes a selector to
 *  be "everything since the last `}`", so comments and at-rule blocks must go
 *  first — otherwise a comment glues onto the next selector and an at-rule's
 *  inner rules flatten into the same list, both of which silently retarget the
 *  lookups depending on where in the file things happen to sit. */
function topLevelRules(css: string): [string, string][] {
  const flat = css.replace(COMMENT, "").replace(AT_STATEMENT, "").replace(AT_RULE, "");
  return [...flat.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .map(([, selector, body]) => [selector.trim(), body] as [string, string]);
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
// rule that carries the flash must name it too — dropping `.panel` from a
// selector list is invisible to a `.cell`-only assertion.
const TARGETS = [".cell.active", ".panel.active"];

describe("press flash styling", () => {
  const rules = topLevelRules(STYLE);
  const base = rules.find(([sel]) => sel.includes(".cell.active") && !sel.includes(".alt"));
  const alt = rules.find(([sel]) => sel.includes(".cell.active.alt"));

  it("restarts the flash by swapping the animation name", () => {
    expect(base?.[1], "no base rule for the pressed cell").toMatch(/animation:\s*press-a/);
    expect(alt?.[1], "no parity rule to restart the animation").toMatch(/animation-name:\s*press-b/);
  });

  it("flashes the panel as well as the tiles", () => {
    expect(base?.[0], "the base flash skips the panel").toContain(".panel.active");
    expect(alt?.[0], "the parity retrigger skips the panel").toContain(".panel.active.alt");
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
  it("still distinguishes consecutive presses under reduced motion", () => {
    const reduced = atRuleBody(STYLE, "@media (prefers-reduced-motion: reduce)");
    expect(reduced, "no reduced-motion rule for the press parity").not.toBeNull();

    const parity = topLevelRules(reduced!).find(([sel]) => sel.includes(".cell.active.alt"));
    expect(parity, "reduced motion leaves the parity with no visual difference").toBeDefined();
    for (const target of TARGETS) {
      expect(parity![0], `reduced motion skips ${target}`).toContain(`${target}.alt`);
    }

    const plain = ringColour(base?.[1] ?? "");
    const alternate = ringColour(parity![1]);
    expect(plain, "the base rule paints no ring colour to differ from").toBeTruthy();
    expect(alternate, "the reduced-motion parity paints no ring colour").toBeTruthy();
    expect(alternate, "both presses paint the same ring, so they look identical")
      .not.toBe(plain);
  });
});
