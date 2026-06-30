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
 */
export function fitDecision(
  scrollHeight: number,
  lastRequestedHeight: number | null,
  width: number,
  tolerance = 1,
): FitDecision {
  const height = Math.round(scrollHeight);
  if (lastRequestedHeight !== null && Math.abs(height - lastRequestedHeight) <= tolerance) {
    return { apply: false, width, height: lastRequestedHeight };
  }
  return { apply: true, width, height };
}
