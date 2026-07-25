export type AppSurface = "desktop" | "compact";

/**
 * The normal macOS window is the full desktop control room. The deck-only
 * surface is reserved for the two compact overlay modes.
 */
export function appSurface(windowMode: string | null | undefined): AppSurface {
  return windowMode === "floating" || windowMode === "always_on_top"
    ? "compact"
    : "desktop";
}
