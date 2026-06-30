import { describe, it, expect } from "vitest";
import {
  parseSetupStatus,
  onboardingDecision,
  shouldOnboard,
  type SetupStatus,
} from "./onboardingClient";

const full = {
  mode: "remote",
  connected: true,
  reason: null,
  local_herdr_available: false,
  saved_remote_available: false,
  choice: null,
  socket_path: "/home/u/.config/herdr/herdr.sock",
};

describe("parseSetupStatus", () => {
  it("shapes a full status (snake_case -> camelCase)", () => {
    const s = parseSetupStatus(full);
    expect(s).not.toBeNull();
    expect(s?.mode).toBe("remote");
    expect(s?.connected).toBe(true);
    expect(s?.reason).toBeNull();
    expect(s?.localHerdrAvailable).toBe(false);
    expect(s?.choice).toBeNull();
    expect(s?.socketPath).toBe("/home/u/.config/herdr/herdr.sock");
  });

  it("keeps a string reason and a local_herdr_available true", () => {
    const s = parseSetupStatus({ ...full, reason: "first_run", local_herdr_available: true });
    expect(s?.reason).toBe("first_run");
    expect(s?.localHerdrAvailable).toBe(true);
  });

  it("returns null for non-objects / missing mode", () => {
    expect(parseSetupStatus(null)).toBeNull();
    expect(parseSetupStatus(undefined)).toBeNull();
    expect(parseSetupStatus("nope")).toBeNull();
    expect(parseSetupStatus({ connected: true })).toBeNull(); // no mode
  });

  it("defaults soft fields when absent or wrong-typed", () => {
    const s = parseSetupStatus({ mode: "mock" });
    expect(s).not.toBeNull();
    expect(s?.connected).toBe(false);
    expect(s?.reason).toBeNull();
    expect(s?.localHerdrAvailable).toBe(false);
    expect(s?.choice).toBeNull();
    expect(s?.socketPath).toBe("");
  });

  it("maps saved_remote_available -> savedRemoteAvailable (true)", () => {
    const s = parseSetupStatus({ ...full, saved_remote_available: true });
    expect(s?.savedRemoteAvailable).toBe(true);
  });

  it("defaults savedRemoteAvailable to false when absent or wrong-typed", () => {
    expect(parseSetupStatus({ mode: "mock" })?.savedRemoteAvailable).toBe(false);
    expect(parseSetupStatus({ mode: "mock", saved_remote_available: "yes" })?.savedRemoteAvailable).toBe(false);
  });
});

describe("onboardingDecision (exhaustive on reason, defaults to deck)", () => {
  const at = (reason: string | null): SetupStatus => ({
    mode: "mock",
    connected: false,
    reason,
    localHerdrAvailable: true,
    savedRemoteAvailable: false,
    choice: null,
    socketPath: "",
  });

  it("first_run -> welcome", () => {
    expect(onboardingDecision(at("first_run"))).toBe("welcome");
  });

  it("local_unavailable -> reconnect", () => {
    expect(onboardingDecision(at("local_unavailable"))).toBe("reconnect");
  });

  it("connected/null, demo, mock_env, unknown -> deck", () => {
    expect(onboardingDecision(at(null))).toBe("deck");
    expect(onboardingDecision(at("demo"))).toBe("deck");
    expect(onboardingDecision(at("mock_env"))).toBe("deck");
    expect(onboardingDecision(at("something_new"))).toBe("deck");
  });

  it("null status (not ready / unreadable) -> deck", () => {
    expect(onboardingDecision(null)).toBe("deck");
  });
});

describe("shouldOnboard (manual re-onboarding override)", () => {
  const demo: SetupStatus = {
    mode: "mock",
    connected: false,
    reason: "demo",
    localHerdrAvailable: true,
    savedRemoteAvailable: false,
    choice: "demo",
    socketPath: "",
  };

  it("forces the welcome card over a deck decision when override is set", () => {
    expect(shouldOnboard(demo, false)).toBe("deck");
    expect(shouldOnboard(demo, true)).toBe("welcome");
  });

  it("keeps a real reconnect card even under override", () => {
    const recon = { ...demo, reason: "local_unavailable" };
    expect(shouldOnboard(recon, true)).toBe("reconnect");
    expect(shouldOnboard(recon, false)).toBe("reconnect");
  });
});

import {
  parseConnectResult,
  setupTransport,
  type ConnectRequest,
} from "./onboardingClient";
import type { InvokeFn } from "./deckClient";

describe("parseConnectResult", () => {
  it("shapes a success", () => {
    const r = parseConnectResult({ ok: true, connected: true });
    expect(r).toEqual({ ok: true, connected: true, error: null });
  });

  it("shapes a failure with an error reason", () => {
    const r = parseConnectResult({ ok: false, error: "bad_token" });
    expect(r).toEqual({ ok: false, connected: false, error: "bad_token" });
  });

  it("treats garbage as a non-ok result (never throws)", () => {
    expect(parseConnectResult(null)).toEqual({ ok: false, connected: false, error: null });
    expect(parseConnectResult("nope")).toEqual({ ok: false, connected: false, error: null });
  });
});

describe("setupTransport", () => {
  it("status() invokes setup_status and parses the result", async () => {
    const calls: { cmd: string; args?: Record<string, unknown> }[] = [];
    const invoke = async (cmd: string, args?: Record<string, unknown>) => {
      calls.push({ cmd, args });
      return { mode: "mock", reason: "first_run", local_herdr_available: true };
    };
    const t = setupTransport(invoke);
    const s = await t.status();
    expect(calls).toEqual([{ cmd: "setup_status", args: undefined }]);
    expect(s?.mode).toBe("mock");
    expect(s?.reason).toBe("first_run");
  });

  it("status() returns null when invoke rejects (outside the WebView)", async () => {
    const invoke = async () => {
      throw new Error("no tauri");
    };
    expect(await setupTransport(invoke).status()).toBeNull();
  });

  it("connect() forwards the request as the `body` arg and parses the result", async () => {
    const calls: { cmd: string; args?: Record<string, unknown> }[] = [];
    const invoke = async (cmd: string, args?: Record<string, unknown>) => {
      calls.push({ cmd, args });
      return { ok: true, connected: true };
    };
    const req: ConnectRequest = { choice: "remote", url: "ws://h:8788", token: "tok", id: "herdr" };
    const r = await setupTransport(invoke).connect(req);
    expect(calls).toEqual([{ cmd: "setup_connect", args: { body: req } }]);
    expect(r).toEqual({ ok: true, connected: true, error: null });
  });

  it("connect() surfaces a thrown command (non-200) as a non-ok result", async () => {
    const invoke = async () => {
      throw new Error("sidecar returned HTTP 503 for /setup/connect");
    };
    const r = await setupTransport(invoke).connect({ choice: "demo" });
    expect(r.ok).toBe(false);
    expect(r.error).toContain("HTTP 503");
  });

  it("connect() forwards a saved request as the body arg", async () => {
    let seen: unknown;
    const invoke = (async (_cmd: string, args?: unknown) => {
      seen = args;
      return { ok: true, connected: false };
    }) as InvokeFn;
    const t = setupTransport(invoke);
    const r = await t.connect({ choice: "saved" });
    expect(seen).toEqual({ body: { choice: "saved" } });
    expect(r.ok).toBe(true);
  });
});
