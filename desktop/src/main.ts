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
  /* not in a Tauri WebView */
}

const Root = label === "config" ? ConfigApp : App;
const app = mount(Root, { target: document.getElementById("app")! });

export default app;
