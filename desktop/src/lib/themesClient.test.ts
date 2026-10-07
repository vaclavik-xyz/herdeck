import { describe, expect, it } from "vitest";
import { BUILTIN_DECK_THEMES, fallbackThemes, fetchThemes, parseThemes, themeOptions } from "./themesClient";

const body = [
  ...BUILTIN_DECK_THEMES.map((id) => ({ id, name: { en: id, cs: id }, builtin: true, error: null })),
  { id: "mine", name: { en: "Mine" }, builtin: false, error: null },
  { id: "broken", name: {}, builtin: false, error: "SyntaxError: invalid syntax" },
  { id: "Bad Id", name: {}, builtin: false, error: null },
  "junk",
];

describe("themesClient", () => {
  it("parses /themes defensively", () => {
    const themes = parseThemes(body)!;
    expect(themes.map((t) => t.id)).toEqual([...BUILTIN_DECK_THEMES, "mine", "broken"]);
    expect(themes[0]).toEqual({ id: BUILTIN_DECK_THEMES[0], name: { en: BUILTIN_DECK_THEMES[0], cs: BUILTIN_DECK_THEMES[0] }, builtin: true, error: null });
    expect(themes[themes.length - 1].error).toBe("SyntaxError: invalid syntax");
    expect(parseThemes({ nope: 1 })).toBeNull();
  });

  it("offers built-ins first and never a broken theme", () => {
    expect(themeOptions(parseThemes(body)!)).toEqual([...BUILTIN_DECK_THEMES, "mine"]);
    expect(themeOptions([])).toEqual([...BUILTIN_DECK_THEMES]);
  });

  it("fetches through the maintenance proxy", async () => {
    const calls: unknown[] = [];
    const invoke = async (cmd: string, args?: Record<string, unknown>) => {
      calls.push([cmd, args]);
      return { status: 200, body };
    };
    const themes = await fetchThemes(invoke);
    expect(calls).toEqual([["maintenance_call", { method: "GET", path: "/themes" }]]);
    expect(themes.map((t) => t.id)).toEqual([...BUILTIN_DECK_THEMES, "mine", "broken"]);
  });

  it("falls back to the built-ins without a runtime", async () => {
    expect(await fetchThemes(null)).toEqual(fallbackThemes());
    expect(await fetchThemes(async () => ({ status: 404, body: null }))).toEqual(fallbackThemes());
    expect(await fetchThemes(async () => { throw new Error("route not allowed"); })).toEqual(fallbackThemes());
  });
});
