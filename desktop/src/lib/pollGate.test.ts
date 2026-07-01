import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { visibilityGatedLoop, type VisibilityDoc } from "./pollGate";

class FakeDoc implements VisibilityDoc {
  hidden = false;
  private listeners: (() => void)[] = [];

  addEventListener(_type: string, fn: EventListenerOrEventListenerObject): void {
    this.listeners.push(fn as () => void);
  }

  removeEventListener(_type: string, fn: EventListenerOrEventListenerObject): void {
    this.listeners = this.listeners.filter((l) => l !== (fn as () => void));
  }

  setHidden(hidden: boolean): void {
    this.hidden = hidden;
    for (const l of [...this.listeners]) l();
  }

  get listenerCount(): number {
    return this.listeners.length;
  }
}

async function flush(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

describe("visibilityGatedLoop", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("steps immediately and reschedules at the interval", async () => {
    const doc = new FakeDoc();
    const steps: number[] = [];
    const stop = visibilityGatedLoop(() => void steps.push(1), () => 300, doc);
    await flush();
    expect(steps.length).toBe(1);
    await vi.advanceTimersByTimeAsync(300);
    expect(steps.length).toBe(2);
    await vi.advanceTimersByTimeAsync(600);
    expect(steps.length).toBe(4);
    stop();
  });

  it("parks while hidden and does one immediate step on show", async () => {
    const doc = new FakeDoc();
    let steps = 0;
    const stop = visibilityGatedLoop(() => void steps++, () => 300, doc);
    await flush();
    expect(steps).toBe(1);
    doc.setHidden(true);
    await vi.advanceTimersByTimeAsync(3000);
    expect(steps).toBe(1); // fully parked — no hidden polling
    doc.setHidden(false);
    await flush();
    expect(steps).toBe(2); // immediate refresh on show
    await vi.advanceTimersByTimeAsync(300);
    expect(steps).toBe(3); // and the loop resumed
    stop();
  });

  it("does not reschedule when hidden flips during an in-flight step", async () => {
    const doc = new FakeDoc();
    let steps = 0;
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    const stop = visibilityGatedLoop(
      async () => {
        steps++;
        await gate;
      },
      () => 300,
      doc,
    );
    await flush();
    expect(steps).toBe(1);
    doc.hidden = true; // window ordered out mid-step (no event needed)
    release();
    await flush();
    await vi.advanceTimersByTimeAsync(3000);
    expect(steps).toBe(1); // parked after the in-flight step completed
    stop();
  });

  it("never overlaps steps when a spurious visibility event fires mid-step", async () => {
    const doc = new FakeDoc();
    let active = 0;
    let maxActive = 0;
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    const stop = visibilityGatedLoop(
      async () => {
        active++;
        maxActive = Math.max(maxActive, active);
        await gate;
        active--;
      },
      () => 300,
      doc,
    );
    await flush();
    doc.setHidden(false); // spurious "visible" while step 1 still runs
    await flush();
    release();
    await flush();
    expect(maxActive).toBe(1);
    stop();
  });

  it("stop cancels the loop and removes the listener", async () => {
    const doc = new FakeDoc();
    let steps = 0;
    const stop = visibilityGatedLoop(() => void steps++, () => 300, doc);
    await flush();
    stop();
    await vi.advanceTimersByTimeAsync(3000);
    expect(steps).toBe(1);
    expect(doc.listenerCount).toBe(0);
  });
});
