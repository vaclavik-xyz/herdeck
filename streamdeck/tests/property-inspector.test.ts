import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const html = readFileSync(
  fileURLToPath(new URL("../xyz.vaclavik.herdeck.sdPlugin/ui/herdeck.html", import.meta.url)),
  "utf8",
);

describe("Property Inspector (offline-first)", () => {
  it("has no external/CDN dependency — no http(s) URL, no external <script src>", () => {
    expect(html).not.toMatch(/https?:\/\//i); // no http/https URL anywhere (CDN, schema, etc.)
    expect(html).not.toMatch(/<script[^>]*\bsrc=/i); // no external script tag at all
  });

  it("wires the Stream Deck PI websocket protocol against GLOBAL settings", () => {
    // The Stream Deck app calls this global entry point with the registration args.
    expect(html).toContain("connectElgatoStreamDeckSocket");
    // Loopback websocket only (the app's local registration socket), never a remote host.
    expect(html).toMatch(/ws:\/\/127\.0\.0\.1/);
    expect(html).toContain("getGlobalSettings");
    expect(html).toContain("setGlobalSettings");
    expect(html).toContain("didReceiveGlobalSettings");
    expect(html).toContain("herdeckPath");
  });
});
