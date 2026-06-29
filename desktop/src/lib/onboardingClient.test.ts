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
});

describe("onboardingDecision (exhaustive on reason, defaults to deck)", () => {
  const at = (reason: string | null): SetupStatus => ({
    mode: "mock",
    connected: false,
    reason,
    localHerdrAvailable: true,
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
