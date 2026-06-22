import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { ACTION_UUIDS } from "../src/actions/core.js";

const manifest = JSON.parse(
  readFileSync(fileURLToPath(new URL("../xyz.vaclavik.herdeck.sdPlugin/manifest.json", import.meta.url)), "utf8"),
);

describe("manifest.json", () => {
  it("declares the herdeck plugin with a Node code path, mac-only (Unix socket transport)", () => {
    expect(manifest.UUID).toBe("xyz.vaclavik.herdeck");
    expect(manifest.CodePath).toBe("bin/plugin.js");
    expect(manifest.Nodejs?.Version).toBeTruthy();
    expect(manifest.SDKVersion).toBe(2);
    expect(manifest.OS.map((o: any) => o.Platform)).toEqual(["mac"]); // Windows needs a non-Unix-socket transport (follow-up)
  });

  it("declares all five herdr actions, Keypad-only, under the herdr category", () => {
    const uuids = manifest.Actions.map((a: any) => a.UUID).sort();
    expect(uuids).toEqual(Object.values(ACTION_UUIDS).slice().sort());
    for (const a of manifest.Actions) {
      expect(a.Controllers).toEqual(["Keypad"]);
      expect(Array.isArray(a.States) && a.States.length >= 1).toBe(true);
    }
    expect(manifest.Category).toBe("herdr");
  });

  it("Approve/Deny/Stop/Pager declare the Property Inspector for the herdeck path", () => {
    expect(manifest.PropertyInspectorPath).toBe("ui/herdeck.html");
  });
});
