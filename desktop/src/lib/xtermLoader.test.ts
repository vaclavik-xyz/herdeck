import { describe, it, expect } from "vitest";
import { frameBytes, loadXtermModules } from "./xtermLoader";

describe("xtermLoader", () => {
  // Guards the reuse of the dashboard's vendored UMD bundles through the
  // `@herdeck-web` alias: a moved file or a bundler change that hides the
  // constructors must fail here, not as a blank terminal in the app.
  it("resolves the vendored Terminal and FitAddon constructors", async () => {
    const { Terminal, FitAddon } = await loadXtermModules();
    expect(typeof Terminal).toBe("function");
    expect(typeof FitAddon).toBe("function");
  });

  it("decodes a base64 frame payload to raw bytes", () => {
    expect([...frameBytes(btoa("\x1b[1mhi"))]).toEqual([27, 91, 49, 109, 104, 105]);
  });
});
