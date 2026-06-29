import { describe, it, expect } from "vitest";
import Onboarding from "./Onboarding.svelte";

// Compile-smoke only (matches fields/widgets.smoke.test.ts): importing the
// .svelte compiles it, catching syntax/compile errors without a render harness.
// The card's behavior lives in onboardingClient.ts and is unit-tested there.
describe("Onboarding compile-smoke", () => {
  it("compiles the onboarding card", () => {
    expect(Onboarding).toBeTruthy();
  });
});
