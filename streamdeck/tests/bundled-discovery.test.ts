import { describe, it, expect } from "vitest";
import { resolveHerdeckCommand, bundledBackendPath } from "../src/backend-process.js";

const yes = () => true;
const no = () => false;

describe("resolveHerdeckCommand bundled precedence", () => {
  it("PI path and HERDECK_BIN win over a present bundled binary", () => {
    expect(resolveHerdeckCommand({ configuredPath: "/opt/h", bundledPath: "/b", exists: yes }))
      .toEqual({ command: "/opt/h", args: [] });
    expect(resolveHerdeckCommand({ envBin: "/usr/bin/herdeck", bundledPath: "/b", exists: yes }))
      .toEqual({ command: "/usr/bin/herdeck", args: [] });
  });

  it("uses the bundled binary when present and no override is set", () => {
    expect(resolveHerdeckCommand({ bundledPath: "/plugin/backend/herdeck-backend/herdeck-backend", exists: yes }))
      .toEqual({ command: "/plugin/backend/herdeck-backend/herdeck-backend", args: [] });
  });

  it("falls through to PATH when the bundled binary is absent (dev checkout)", () => {
    expect(resolveHerdeckCommand({ bundledPath: "/plugin/backend/herdeck-backend/herdeck-backend", exists: no }))
      .toEqual({ command: "herdeck", args: [] });
  });

  it("with nothing set, resolves to herdeck on PATH", () => {
    expect(resolveHerdeckCommand({ exists: no })).toEqual({ command: "herdeck", args: [] });
  });
});

describe("bundledBackendPath", () => {
  it("derives ../backend/herdeck-backend/herdeck-backend from plugin.js URL", () => {
    const url = "file:///Users/x/xyz.vaclavik.herdeck.sdPlugin/bin/plugin.js";
    expect(bundledBackendPath(url)).toBe(
      "/Users/x/xyz.vaclavik.herdeck.sdPlugin/backend/herdeck-backend/herdeck-backend",
    );
  });
});
