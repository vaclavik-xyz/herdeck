/** A content-fit sizing decision for the borderless deck window. */
export interface FitDecision {
  apply: boolean;
  width: number;
  height: number;
}

/**
 * Decide the next window size from the measured intrinsic content height.
 *
 * Rounds to integer logical px and SKIPS (`apply:false`) when the new height is
 * within `tolerance` px of the last requested height — the anti-feedback guard
 * that stops `setSize -> viewport change -> ResizeObserver -> setSize`
 * oscillation. `width` is passed through unchanged (the borderless window has a
 * fixed, non-resizable width).
 *
 * A height that is not a positive number is SKIPPED too, and that is the last
 * thing standing between a bad measurement and a zero-height window: the deck
 * window is created hidden, its ResizeObserver attaches on mount either way, and
 * a WebView that has not laid out on screen yet can report 0. Nothing
 * downstream floors it — `setSize(width, 0)` would leave the user's first "show
 * deck" painting nothing at all.
 */
export function fitDecision(
  scrollHeight: number,
  lastRequestedHeight: number | null,
  width: number,
  tolerance = 1,
): FitDecision {
  const height = Math.round(scrollHeight);
  // Written as a positive test so NaN skips as well.
  if (!(height > 0)) {
    return { apply: false, width, height: lastRequestedHeight ?? 0 };
  }
  if (lastRequestedHeight !== null && Math.abs(height - lastRequestedHeight) <= tolerance) {
    return { apply: false, width, height: lastRequestedHeight };
  }
  return { apply: true, width, height };
}
