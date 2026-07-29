import "./lib/theme.css";
import { mount } from "svelte";
import App from "./App.svelte";

// Both windows load index.html and mount the same root: App.svelte routes its
// surface off `<html data-window-role>`, which Rust's initialization_script
// stamps before this module even runs (see window_role_script in
// desktop/src-tauri/src/lib.rs). There is nothing left for main.ts to route in
// a real Tauri WebView.
//
// A plain browser (no Tauri) never gets that attribute, so `?window=` lets
// dev/design work pick a surface without one. Values mirror the real Tauri
// window labels — "config" (APP_WINDOW, the settings surface) and "main"
// (DECK_WINDOW, the compact deck) — rather than the raw role strings, so a
// bookmarked/documented URL (desktop/README.md's `?window=config`) keeps
// working unchanged. Anything else, including no query at all, leaves the
// attribute unset and App.svelte's own default ("app") applies: the settings
// surface is the one worth designing against in a plain browser.
const QUERY_WINDOW_ROLE: Record<string, string> = { config: "app", main: "deck" };

try {
  const { getCurrentWindow } = await import("@tauri-apps/api/window");
  getCurrentWindow();
} catch {
  const label = new URLSearchParams(window.location.search).get("window") ?? "";
  const role = QUERY_WINDOW_ROLE[label];
  if (role) document.documentElement.dataset.windowRole = role;
}

const app = mount(App, { target: document.getElementById("app")! });

export default app;
