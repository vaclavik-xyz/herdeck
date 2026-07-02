// Visibility-gated polling loop. The deck lives in the tray: windows are hidden
// far more than shown, and a hidden WebView polling at full rate burns CPU and
// battery for pixels nobody sees. This loop parks completely while the document
// is hidden (WKWebView flips `document.hidden` when the window is ordered out)
// and fires one immediate step on return to visibility.

/** The subset of Document the gate needs — injectable for tests. */
export type VisibilityDoc = Pick<
  Document,
  "hidden" | "addEventListener" | "removeEventListener"
>;

/** Handle to a running gated loop. */
export type GatedLoop = {
  stop: () => void;
  /** Run a step immediately (e.g. right after a press the server already
   * applied) instead of waiting out the current interval. No-op while hidden
   * or stopped; never overlaps an in-flight step. */
  kick: () => void;
};

/**
 * Run `step` immediately and then every `intervalMs()` while the document is
 * visible; park while hidden; one immediate step on show. Steps never overlap
 * (the next is scheduled only after the previous resolves — setInterval would
 * let a slow poll clobber a newer one).
 */
export function visibilityGatedLoop(
  step: () => Promise<void> | void,
  intervalMs: () => number,
  doc: VisibilityDoc = document,
): GatedLoop {
  let stopped = false;
  let running = false;
  let kickPending = false; // a kick that arrived while a step was in flight
  let timer: ReturnType<typeof setTimeout> | undefined;

  async function run(): Promise<void> {
    if (stopped || doc.hidden || running) return;
    running = true;
    try {
      await step();
    } finally {
      running = false;
    }
    if (stopped || doc.hidden) {
      kickPending = false;
      return;
    }
    if (kickPending) {
      // e.g. a press completed while the scheduled poll was in flight — the
      // in-flight result predates the press, so refresh immediately.
      kickPending = false;
      void run();
      return;
    }
    timer = setTimeout(() => void run(), intervalMs());
  }

  function onVisibility(): void {
    if (timer) {
      clearTimeout(timer);
      timer = undefined;
    }
    if (!doc.hidden) void run(); // immediate refresh on show; parked while hidden
  }

  function kick(): void {
    if (stopped || doc.hidden) return;
    if (running) {
      kickPending = true; // honoured the moment the in-flight step resolves
      return;
    }
    if (timer) {
      clearTimeout(timer);
      timer = undefined;
    }
    void run();
  }

  doc.addEventListener("visibilitychange", onVisibility);
  void run();
  return {
    kick,
    stop: () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      doc.removeEventListener("visibilitychange", onVisibility);
    },
  };
}
