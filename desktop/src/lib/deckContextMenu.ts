import type { FloatingScaleCommand } from "./floatingScale";

/** Emitted by Rust to the DECK window only (`FLOATING_ZOOM_EVENT` /
 *  `app.emit_to(DECK_WINDOW, ...)` in desktop/src-tauri/src/lib.rs) when a
 *  zoom item is picked from the deck's native right-click context menu. */
export const DECK_ZOOM_EVENT = "floating-zoom-command";

/**
 * Validate a `DECK_ZOOM_EVENT` payload before handing it to
 * `applyFloatingScale`. Rust only ever emits one of the three commands, but
 * the IPC boundary is not statically typed on this side, so an unrecognised
 * payload is dropped here instead of silently mis-zooming —
 * `changeFloatingScale` treats any string other than "in"/"reset" as "out".
 */
export function floatingScaleCommandFromEvent(payload: unknown): FloatingScaleCommand | null {
  return payload === "in" || payload === "out" || payload === "reset" ? payload : null;
}

/**
 * Whether a `contextmenu` event on the deck should fall through to the
 * WebView's own (native/dev) context menu instead of popping the Rust-built
 * deck menu. Shift+right-click is the escape hatch that keeps "Inspect
 * Element" reachable once the deck's plain right-click is claimed by the
 * custom menu.
 */
export function shouldBypassDeckContextMenu(event: { shiftKey: boolean }): boolean {
  return event.shiftKey;
}
