import { describe, it, expect } from "vitest";
import { asDiscovery, parseHealth } from "./sidecar";

describe("asDiscovery", () => {
  it("accepts a well-formed discovery view (token-free)", () => {
    const d = asDiscovery({
      url: "http://127.0.0.1:51234",
      host: "127.0.0.1",
      port: 51234,
      source: "mock",
    });
    expect(d).not.toBeNull();
    expect(d?.url).toBe("http://127.0.0.1:51234");
    expect(d?.port).toBe(51234);
    expect(d?.source).toBe("mock");
    // the token must never reach JS in this slice
    expect((d as Record<string, unknown>).token).toBeUndefined();
  });

  it("returns null for null / not-ready-yet", () => {
    expect(asDiscovery(null)).toBeNull();
    expect(asDiscovery(undefined)).toBeNull();
  });

  it("returns null when required fields are missing", () => {
    expect(asDiscovery({ url: "http://x", token: "t" })).toBeNull(); // no source
    expect(asDiscovery({ token: "t", source: "mock" })).toBeNull(); // no url
  });
});

describe("parseHealth", () => {
  it("shapes a healthy mock response", () => {
    const r = parseHealth({
      ok: true,
      source: "mock",
      connected: false,
      server_id: null,
    });
    expect(r.ok).toBe(true);
    expect(r.source).toBe("mock");
    expect(r.connected).toBe(false);
    expect(r.serverId).toBeNull();
  });

  it("maps server_id -> serverId and connected:true for live", () => {
    const r = parseHealth({
      ok: true,
      source: "live",
      connected: true,
      server_id: "srv-7",
    });
    expect(r.source).toBe("live");
    expect(r.connected).toBe(true);
    expect(r.serverId).toBe("srv-7");
  });

  it("defaults safely on missing / junk fields", () => {
    const r = parseHealth({});
    expect(r.ok).toBe(false);
    expect(r.source).toBe("unknown");
    expect(r.connected).toBe(false);
    expect(r.serverId).toBeNull();
    expect(parseHealth(null).source).toBe("unknown");
  });
});
