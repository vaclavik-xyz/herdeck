import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// The version the UI shows comes from package.json at build time, never from a
// literal in a component. scripts/set-version.py keeps package.json in step with
// VERSION and the other manifests, and tests/test_version_consistency.py enforces
// it — a hand-written string in a .svelte file is outside all of that, which is
// exactly how the settings sidebar came to claim v0.1.1 while the app shipped 0.2.0.
const appVersion: string = JSON.parse(
  readFileSync(fileURLToPath(new URL("./package.json", import.meta.url)), "utf8"),
).version;

// Vite + Svelte for the WebView frontend, plus Vitest for the unit tests.
// The build output (`build/`) is what Tauri embeds as `frontendDist`.
export default defineConfig({
  plugins: [svelte()],
  define: {
    __APP_VERSION__: JSON.stringify(appVersion),
  },
  // Under Vitest, resolve Svelte's BROWSER build: the default node resolution
  // picks index-server.js, whose mount() throws — and the component-mounting
  // tests (sections.help.test.ts) need a real client-side mount in jsdom.
  resolve: process.env.VITEST ? { conditions: ["browser"] } : undefined,
  // Tauri drives the dev server; don't let Vite clear its logs.
  clearScreen: false,
  build: {
    outDir: "build",
    emptyOutDir: true,
    target: "esnext",
  },
  server: {
    port: 1420,
    strictPort: true,
    // Bind all interfaces so the dev server is reachable across the tailnet
    // (per repo convention); the Tauri WebView still connects via localhost.
    host: "0.0.0.0",
  },
  test: {
    // Logic-only unit tests (health/discovery helpers). The full DeckView
    // component tests (poll/diff/press) belong to slice 2.
    environment: "jsdom",
    include: ["src/**/*.{test,spec}.ts"],
  },
});
