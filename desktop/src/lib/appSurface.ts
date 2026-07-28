export type AppSurface = "desktop" | "compact";
export type WindowRole = "app" | "deck";

/** The deck window hosts the compact overlay; everything else is the control
 *  room. Routing by ROLE rather than by a window mode is the point of this
 *  design: a window's role never changes, so its surface never changes either. */
export function appSurface(role: string | null | undefined): AppSurface {
  return role === "deck" ? "compact" : "desktop";
}

/** Reads the role Rust stamps on `<html data-window-role>` before first paint
 *  (`window_role_script` in desktop/src-tauri/src/lib.rs, injected via
 *  `initialization_script` so the borderless CSS applies with no flash of
 *  opaque chrome). The dataset key ("windowRole") and both literal values
 *  ("app" / "deck") are a contract with that Rust code — see
 *  appSurface.test.ts for the other half of the pin. `App.svelte` calls this
 *  rather than reading `document.documentElement.dataset` inline, so the one
 *  place that knows the attribute's name is also the one place tested against
 *  it. */
export function windowRole(doc: Document | undefined): string | undefined {
  return doc?.documentElement.dataset.windowRole;
}

export function desktopSetupVisible(
  surface: AppSurface,
  view: "deck" | "welcome" | "reconnect",
  hidden: boolean,
): boolean {
  return surface === "desktop" && view !== "deck" && !hidden;
}
