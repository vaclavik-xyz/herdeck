import { mount } from "svelte";
import App from "./App.svelte";
import ConfigApp from "./ConfigApp.svelte";

// Both windows load index.html; pick the root by window label. getCurrentWindow
// throws outside a Tauri WebView (plain browser) — default to the deck there.
let label = "main";
try {
  const { getCurrentWindow } = await import("@tauri-apps/api/window");
  label = getCurrentWindow().label;
} catch {
  // Plain-browser design/dev mode: opt into the settings surface without a
  // Tauri window label. Production WebViews never take this branch.
  if (new URLSearchParams(window.location.search).get("window") === "config") {
    label = "config";
  }
}

const Root = label === "config" ? ConfigApp : App;
const app = mount(Root, { target: document.getElementById("app")! });

export default app;
