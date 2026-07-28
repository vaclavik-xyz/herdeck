// @vitest-environment node
// The retrigger has two halves: the class churn (asserted by mounting, in
// DeckView.press.test.ts) and the CSS that makes it visible. jsdom applies no
// scoped Svelte styles, so this half is asserted from source — otherwise the
// rules could be deleted with every test still green and 13 nodes churning a
// class for nothing.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, it, expect } from "vitest";

describe("press flash styling", () => {
  const source = readFileSync(fileURLToPath(new URL("./DeckView.svelte", import.meta.url)), "utf8");
  const style = source.match(/<style>([\s\S]*)<\/style>/)?.[1] ?? "";

  const rules = (css: string): [string, string][] =>
    [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(([, sel, body]) => [sel.trim(), body]);

  it("restarts the flash by swapping the animation name", () => {
    const all = rules(style);
    const base = all.find(([sel]) => sel.includes(".cell.active") && !sel.includes(".alt"));
    const alt = all.find(([sel]) => sel.includes(".cell.active.alt"));
    expect(base?.[1], "no base rule for the pressed cell").toMatch(/animation:\s*press-a/);
    expect(alt?.[1], "no parity rule to restart the animation").toMatch(/animation-name:\s*press-b/);
  });

  it("keeps the two keyframes identical, or alternate presses would differ", () => {
    const bodies = [...style.matchAll(/@keyframes press-[ab]\s*\{((?:[^{}]|\{[^{}]*\})*)\}/g)]
      .map((m) => m[1].replace(/\s+/g, " ").trim());
    expect(bodies.length, "expected exactly the two parity keyframes").toBe(2);
    expect(bodies[0]).toBe(bodies[1]);
  });

  it("still distinguishes consecutive presses under reduced motion", () => {
    // theme.css flattens animation-duration to .01ms !important there, so the
    // parity needs a difference that is not an animation at all.
    const reduced = style.slice(style.indexOf("@media (prefers-reduced-motion: reduce)"));
    expect(reduced, "no reduced-motion rule for the press parity").not.toBe("");
    expect(reduced).toMatch(/\.cell\.active\.alt[\s\S]{0,120}?outline-color:/);
  });
});
