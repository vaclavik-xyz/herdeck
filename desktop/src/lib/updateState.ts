// The reconciliation for the manual "check for updates" feature — pure
// `(state, event) -> state`, no DOM, no Tauri — pulled out of App.svelte for
// the same reason floatingScale.ts/windowFit.ts/pollGate.ts/connectionStatus.ts
// exist: a decision this shaped belongs next to a plain unit test, not inline
// in a component where it can only be reached through mount() + a mocked
// bridge + hand-held deferred promises.
//
// Two orthogonal fields replace what used to be a single priority puzzle
// (a generation counter, an accumulating set of retracted versions, and a
// three-way live-state compare):
//   - `availableUpdate` is STICKY: once a check finds one, it stays until a
//     LATER check confirms up-to-date, or an install resolves it away.
//   - `notice` is TRANSIENT: "checking" / "up-to-date" / "failed" render on
//     top of (and are auto-dismissed independently of) whatever
//     `availableUpdate` says underneath.
// A stale in-flight result can never downgrade a live one because they are
// different variables — no anti-downgrade guard is needed at all. The
// monotonic `checkSeq` is the ONLY protection against overlapping checks and
// checks racing an install resolution: whichever settles while `checkSeq`
// has already moved on is dropped, unconditionally. The accepted cost: a
// check that was straddling a resolution and discovers a genuinely NEWER
// version at that exact moment is dropped too — the user re-checks.
import type { UpdateCheckState, UpdateInfo } from "./updateClient";

export type Notice =
  | { kind: "checking" }
  | { kind: "up-to-date" }
  | { kind: "failed"; reason: string };

export type UpdateState = {
  availableUpdate: UpdateInfo | null;
  notice: Notice | null;
  checkSeq: number;
};

export function initialUpdateState(): UpdateState {
  return { availableUpdate: null, notice: null, checkSeq: 0 };
}

/** Start a check: claim the next epoch, and for a manual one show "checking"
 *  right away (an automatic check stays silent even while running — nothing
 *  renders for it until `applyCheckResult` decides there's something worth
 *  showing). The caller awaits its transport, then passes the returned `seq`
 *  back into `applyCheckResult` — a later `seq` will have already moved
 *  `checkSeq` on, which is what makes THIS result stale. */
export function beginCheck(state: UpdateState, manual: boolean): { state: UpdateState; seq: number } {
  const seq = state.checkSeq + 1;
  return {
    state: { ...state, checkSeq: seq, notice: manual ? { kind: "checking" } : state.notice },
    seq,
  };
}

/** Apply a settled check's result — unless `seq` no longer matches the live
 *  epoch, meaning another check or an install resolution has already moved
 *  on since this one started; then it is dropped outright, no comparison
 *  needed. `manual` decides whether a non-"available" outcome is worth
 *  surfacing at all (an automatic check stays silent unless it found one). */
export function applyCheckResult(
  state: UpdateState,
  seq: number,
  manual: boolean,
  result: UpdateCheckState,
): UpdateState {
  if (seq !== state.checkSeq) return state;
  switch (result.kind) {
    case "available":
      return { ...state, availableUpdate: result.info, notice: null };
    case "up-to-date":
      // A confirmed up-to-date legitimately retracts a stale "available":
      // if the release was pulled or already applied, the install button is
      // for something that no longer exists.
      return { ...state, availableUpdate: null, notice: manual ? { kind: "up-to-date" } : state.notice };
    case "failed":
      // A failure knows nothing new — it neither confirms nor retracts a
      // known update, so `availableUpdate` is left exactly as it was.
      return { ...state, notice: manual ? { kind: "failed", reason: result.reason } : state.notice };
    case "checking":
      return state;
  }
}

/** A broadcast "available" from the other window (or this window's own
 *  check, echoed back to itself — a real Tauri `emit` reaches its sender
 *  too) is authoritative information, not a result racing this window's own
 *  epoch — apply it unconditionally. */
export function applyAvailableBroadcast(state: UpdateState, info: UpdateInfo): UpdateState {
  return { ...state, availableUpdate: info, notice: null };
}

/** installUpdate resolved with nothing left to install (the updater's own
 *  re-check found the release gone or already applied — see App.svelte for
 *  the full argument on why a successful install can never reach here).
 *  Bumps the epoch unconditionally, even when there was no live
 *  `availableUpdate` to retract (installError's retry action offers the
 *  same install regardless of state): a check left in flight in EITHER
 *  window when this runs must not un-clear it once it settles, and a
 *  "checking" placeholder must not be stranded — since this write already
 *  guarantees that check's own result will be dropped, overwriting
 *  "checking" here (with "up to date": there is nothing left to install) is
 *  what keeps it from sitting there forever. */
export function applyResolvedAway(state: UpdateState): UpdateState {
  return { ...state, availableUpdate: null, notice: { kind: "up-to-date" }, checkSeq: state.checkSeq + 1 };
}

/** Whether `notice` is a dead end nothing else ever clears (auto-dismissed
 *  on a timer by App.svelte) — as opposed to "checking", which always
 *  resolves on its own once the check it stands for actually settles. */
export function isDismissableNotice(notice: Notice | null): boolean {
  return notice?.kind === "up-to-date" || notice?.kind === "failed";
}
