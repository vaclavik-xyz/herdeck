// A transition table for the update-check reconciliation — the same
// coverage App.test.ts used to reach only through mount() + a mocked Tauri
// bridge + hand-held deferred promises now runs as a plain function call.
import { describe, expect, it } from "vitest";
import {
  applyAvailableBroadcast,
  applyCheckResult,
  applyResolvedAway,
  beginCheck,
  initialUpdateState,
  isDismissableNotice,
  type UpdateState,
} from "./updateState";

const info = { version: "0.2.0", current_version: "0.1.0" };
const newerInfo = { version: "0.3.0", current_version: "0.1.0" };

describe("initialUpdateState", () => {
  it("starts with nothing known and epoch 0", () => {
    expect(initialUpdateState()).toEqual({ availableUpdate: null, notice: null, checkSeq: 0 });
  });
});

describe("beginCheck", () => {
  it("bumps the epoch and shows 'checking' for a manual check", () => {
    const { state, seq } = beginCheck(initialUpdateState(), true);
    expect(seq).toBe(1);
    expect(state).toEqual({ availableUpdate: null, notice: { kind: "checking" }, checkSeq: 1 });
  });

  it("bumps the epoch but stays silent for an automatic check", () => {
    const { state, seq } = beginCheck(initialUpdateState(), false);
    expect(seq).toBe(1);
    expect(state).toEqual({ availableUpdate: null, notice: null, checkSeq: 1 });
  });

  it("does not disturb an already-live 'available' banner", () => {
    const before: UpdateState = { availableUpdate: info, notice: null, checkSeq: 3 };
    const { state, seq } = beginCheck(before, true);
    expect(seq).toBe(4);
    // Reporting nothing at all, even from a manual check, was the original
    // defect — but this suite's whole point is that "checking" and
    // "available" are orthogonal, so beginCheck simply follows its own
    // rule (manual -> show checking) regardless of what's already live;
    // applyResolvedAway is the one place that overwrites deliberately.
    expect(state.notice).toEqual({ kind: "checking" });
    expect(state.availableUpdate).toBe(info); // unchanged, still there underneath
  });
});

describe("applyCheckResult", () => {
  it("drops a stale result whose seq no longer matches the live epoch", () => {
    const state: UpdateState = { availableUpdate: info, notice: { kind: "checking" }, checkSeq: 5 };
    const result = applyCheckResult(state, 3, true, { kind: "failed", reason: "offline" });
    expect(result).toBe(state); // untouched, same reference
  });

  it("sets availableUpdate and clears notice on 'available', manual or not", () => {
    for (const manual of [true, false]) {
      const state: UpdateState = { availableUpdate: null, notice: { kind: "checking" }, checkSeq: 1 };
      const result = applyCheckResult(state, 1, manual, { kind: "available", info });
      expect(result).toEqual({ availableUpdate: info, notice: null, checkSeq: 1 });
    }
  });

  it("manual 'up-to-date' clears a live available update and reports the notice", () => {
    const state: UpdateState = { availableUpdate: info, notice: { kind: "checking" }, checkSeq: 1 };
    const result = applyCheckResult(state, 1, true, { kind: "up-to-date" });
    expect(result).toEqual({ availableUpdate: null, notice: { kind: "up-to-date" }, checkSeq: 1 });
  });

  it("automatic 'up-to-date' still clears availableUpdate but stays silent", () => {
    const state: UpdateState = { availableUpdate: info, notice: null, checkSeq: 1 };
    const result = applyCheckResult(state, 1, false, { kind: "up-to-date" });
    expect(result).toEqual({ availableUpdate: null, notice: null, checkSeq: 1 });
  });

  it("manual 'failed' leaves availableUpdate alone and reports the reason", () => {
    const state: UpdateState = { availableUpdate: info, notice: { kind: "checking" }, checkSeq: 1 };
    const result = applyCheckResult(state, 1, true, { kind: "failed", reason: "offline" });
    expect(result).toEqual({
      availableUpdate: info,
      notice: { kind: "failed", reason: "offline" },
      checkSeq: 1,
    });
  });

  it("automatic 'failed' leaves both availableUpdate and notice untouched", () => {
    const state: UpdateState = { availableUpdate: info, notice: null, checkSeq: 1 };
    const result = applyCheckResult(state, 1, false, { kind: "failed", reason: "offline" });
    expect(result).toEqual({ availableUpdate: info, notice: null, checkSeq: 1 });
  });

  it("a 'checking' result (defensive — never actually produced by a settled check) is a no-op", () => {
    const state: UpdateState = { availableUpdate: null, notice: null, checkSeq: 1 };
    const result = applyCheckResult(state, 1, true, { kind: "checking" });
    expect(result).toBe(state);
  });
});

describe("applyAvailableBroadcast", () => {
  it("applies unconditionally, regardless of the live epoch or notice", () => {
    const state: UpdateState = { availableUpdate: null, notice: { kind: "checking" }, checkSeq: 7 };
    const result = applyAvailableBroadcast(state, newerInfo);
    expect(result).toEqual({ availableUpdate: newerInfo, notice: null, checkSeq: 7 });
  });
});

describe("applyResolvedAway", () => {
  it("clears availableUpdate, answers 'up to date', and bumps the epoch", () => {
    const state: UpdateState = { availableUpdate: info, notice: null, checkSeq: 2 };
    const result = applyResolvedAway(state);
    expect(result).toEqual({ availableUpdate: null, notice: { kind: "up-to-date" }, checkSeq: 3 });
  });

  it("still answers and bumps when there was nothing live to resolve (the installError retry path)", () => {
    const state: UpdateState = { availableUpdate: null, notice: null, checkSeq: 0 };
    const result = applyResolvedAway(state);
    expect(result).toEqual({ availableUpdate: null, notice: { kind: "up-to-date" }, checkSeq: 1 });
  });

  it("overwrites (rather than strands) a live 'checking' placeholder", () => {
    // The epoch bump alone would silently strand "checking" forever — the
    // check it stands for is now guaranteed to be dropped by
    // applyCheckResult, and nothing else ever clears "checking" on a timer.
    const state: UpdateState = { availableUpdate: null, notice: { kind: "checking" }, checkSeq: 4 };
    const result = applyResolvedAway(state);
    expect(result.notice).toEqual({ kind: "up-to-date" });
    expect(result.checkSeq).toBe(5);
  });
});

describe("isDismissableNotice", () => {
  it("is true for up-to-date and failed", () => {
    expect(isDismissableNotice({ kind: "up-to-date" })).toBe(true);
    expect(isDismissableNotice({ kind: "failed", reason: "x" })).toBe(true);
  });

  it("is false for checking and null", () => {
    expect(isDismissableNotice({ kind: "checking" })).toBe(false);
    expect(isDismissableNotice(null)).toBe(false);
  });
});
