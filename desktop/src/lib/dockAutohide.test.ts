import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  AUTOHIDE_DELAY_MS,
  AUTOHIDE_REVEAL_MS,
  AUTOHIDE_SLIDE_MS,
  createAutohide,
  sliverStatus,
  type AutohidePhase,
} from "./dockAutohide";
import { dockAgent } from "./dockFixtures";

function machine(slideMs = AUTOHIDE_SLIDE_MS) {
  const phases: AutohidePhase[] = [];
  const ah = createAutohide({ slideMs, onPhase: (p) => phases.push(p) });
  return { ah, phases };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("dock autohide state machine", () => {
  it("hides 1.2 s after the pointer left, sliding out first", () => {
    const { ah, phases } = machine();
    ah.update({ enabled: true, held: false });
    ah.pointer(true);
    ah.pointer(false);
    vi.advanceTimersByTime(AUTOHIDE_DELAY_MS - 1);
    expect(ah.phase).toBe("shown");
    vi.advanceTimersByTime(1);
    expect(ah.phase).toBe("hiding");
    vi.advanceTimersByTime(AUTOHIDE_SLIDE_MS);
    expect(ah.phase).toBe("hidden");
    expect(phases).toEqual(["hiding", "hidden"]);
  });

  it("starts the countdown as soon as it is enabled with the pointer away", () => {
    const { ah } = machine();
    ah.update({ enabled: true, held: false });
    vi.advanceTimersByTime(AUTOHIDE_DELAY_MS + AUTOHIDE_SLIDE_MS);
    expect(ah.phase).toBe("hidden");
  });

  it("a pointer coming back before the delay cancels the hide", () => {
    const { ah } = machine();
    ah.update({ enabled: true, held: false });
    vi.advanceTimersByTime(AUTOHIDE_DELAY_MS - 100);
    ah.pointer(true);
    vi.advanceTimersByTime(AUTOHIDE_DELAY_MS * 3);
    expect(ah.phase).toBe("shown");
  });

  it("does nothing while disabled (autohide off, or the strip not snapped)", () => {
    const { ah, phases } = machine();
    ah.update({ enabled: false, held: false });
    vi.advanceTimersByTime(AUTOHIDE_DELAY_MS * 5);
    expect(ah.phase).toBe("shown");
    expect(phases).toEqual([]);
  });

  it("stays revealed while held (card or group open, keyboard focus, a blocked agent)", () => {
    const { ah } = machine();
    ah.update({ enabled: true, held: true });
    vi.advanceTimersByTime(AUTOHIDE_DELAY_MS * 5);
    expect(ah.phase).toBe("shown");
    // Released: the countdown starts from then, not from before.
    ah.update({ held: false });
    vi.advanceTimersByTime(AUTOHIDE_DELAY_MS - 1);
    expect(ah.phase).toBe("shown");
    vi.advanceTimersByTime(1 + AUTOHIDE_SLIDE_MS);
    expect(ah.phase).toBe("hidden");
  });

  it("a hold (a question arriving) reveals a hidden strip at once", () => {
    const { ah, phases } = machine();
    ah.update({ enabled: true, held: false });
    vi.advanceTimersByTime(AUTOHIDE_DELAY_MS + AUTOHIDE_SLIDE_MS);
    ah.update({ held: true });
    expect(ah.phase).toBe("shown");
    expect(phases).toEqual(["hiding", "hidden", "shown"]);
  });

  it("disabling it (snap lost, pref off) reveals the strip", () => {
    const { ah } = machine();
    ah.update({ enabled: true, held: false });
    vi.advanceTimersByTime(AUTOHIDE_DELAY_MS + AUTOHIDE_SLIDE_MS);
    ah.update({ enabled: false });
    expect(ah.phase).toBe("shown");
  });

  it("the pointer resting on the sliver reveals it after a short dwell", () => {
    const { ah } = machine();
    ah.update({ enabled: true, held: false });
    vi.advanceTimersByTime(AUTOHIDE_DELAY_MS + AUTOHIDE_SLIDE_MS);
    ah.pointer(true);
    vi.advanceTimersByTime(AUTOHIDE_REVEAL_MS - 1);
    expect(ah.phase).toBe("hidden");
    vi.advanceTimersByTime(1);
    expect(ah.phase).toBe("shown");
    // Revealed under the pointer: it stays while the pointer is there.
    vi.advanceTimersByTime(AUTOHIDE_DELAY_MS * 3);
    expect(ah.phase).toBe("shown");
  });

  it("a pointer only passing over the sliver does not reveal it", () => {
    const { ah } = machine();
    ah.update({ enabled: true, held: false });
    vi.advanceTimersByTime(AUTOHIDE_DELAY_MS + AUTOHIDE_SLIDE_MS);
    ah.pointer(true);
    vi.advanceTimersByTime(AUTOHIDE_REVEAL_MS / 2);
    ah.pointer(false);
    vi.advanceTimersByTime(AUTOHIDE_REVEAL_MS * 5);
    expect(ah.phase).toBe("hidden");
  });

  it("the pointer returning while it slides out brings it straight back", () => {
    const { ah } = machine();
    ah.update({ enabled: true, held: false });
    vi.advanceTimersByTime(AUTOHIDE_DELAY_MS + AUTOHIDE_SLIDE_MS / 2);
    expect(ah.phase).toBe("hiding");
    ah.pointer(true);
    expect(ah.phase).toBe("shown");
    vi.advanceTimersByTime(AUTOHIDE_SLIDE_MS * 2);
    expect(ah.phase).toBe("shown");
  });

  it("without motion it hides in one step", () => {
    const { ah, phases } = machine(0);
    ah.update({ enabled: true, held: false });
    vi.advanceTimersByTime(AUTOHIDE_DELAY_MS);
    expect(ah.phase).toBe("hidden");
    expect(phases).toEqual(["hiding", "hidden"]);
  });

  it("destroy stops every timer", () => {
    const { ah, phases } = machine();
    ah.update({ enabled: true, held: false });
    ah.destroy();
    vi.advanceTimersByTime(AUTOHIDE_DELAY_MS * 5);
    expect(phases).toEqual([]);
  });
});

describe("sliverStatus", () => {
  it("is the most urgent status, or null with nobody there", () => {
    expect(sliverStatus([])).toBeNull();
    expect(
      sliverStatus([
        dockAgent({ paneId: "a", status: "idle" }),
        dockAgent({ paneId: "b", status: "working" }),
        dockAgent({ paneId: "c", status: "done" }),
      ]),
    ).toBe("done");
    expect(
      sliverStatus([dockAgent({ paneId: "a", status: "working" }), dockAgent({ paneId: "b", status: "blocked" })]),
    ).toBe("blocked");
  });
});
