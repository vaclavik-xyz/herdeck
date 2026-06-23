<script lang="ts">
  import { onMount } from "svelte";
  import { invoke } from "@tauri-apps/api/core";
  import { listen } from "@tauri-apps/api/event";
  import DeckPlaceholder from "./lib/DeckPlaceholder.svelte";
  import { asDiscovery, parseHealth, type Discovery, type HealthResult } from "./lib/sidecar";

  let status = $state("starting sidecar…");
  let discovery = $state<Discovery | null>(null);
  let health = $state<HealthResult | null>(null);
  let errorMsg = $state<string | null>(null);

  // Ask the Rust shell for the sidecar discovery (url + one-time token). The
  // shell stores the latest discovery and emits a "discovery" event whenever the
  // supervised sidecar (re)starts; we both poll once and subscribe.
  async function pullDiscovery(): Promise<void> {
    try {
      const d = asDiscovery(await invoke("get_discovery"));
      if (d) discovery = d;
    } catch (e) {
      // invoke is unavailable outside the Tauri WebView (e.g. a plain browser);
      // leave discovery null and keep showing the "starting" state.
      errorMsg = `discovery unavailable: ${String(e)}`;
    }
  }

  // Probe /health (via the Rust `check_health` command — see sidecar.ts on why
  // we don't fetch the sidecar directly) to prove the shell can reach it.
  async function pollHealth(): Promise<void> {
    if (!discovery) return;
    try {
      health = parseHealth(await invoke("check_health"));
      errorMsg = null;
      status = `connected to sidecar (${health.source})`;
    } catch (e) {
      health = null;
      status = "sidecar unreachable — reconnecting…";
      errorMsg = String(e);
    }
  }

  onMount(() => {
    let alive = true;

    listen<Discovery>("discovery", (event) => {
      const d = asDiscovery(event.payload);
      if (d) {
        discovery = d;
        void pollHealth();
      }
    });

    // Retry discovery until the supervised sidecar has printed its first line.
    void (async () => {
      while (alive && !discovery) {
        await pullDiscovery();
        if (!discovery) await new Promise((r) => setTimeout(r, 400));
      }
      if (alive) await pollHealth();
    })();

    const healthTimer = setInterval(() => void pollHealth(), 2000);
    return () => {
      alive = false;
      clearInterval(healthTimer);
    };
  });
</script>

<main>
  <!-- PLACEHOLDER: DeckView mounts here (slice 2). For slice 3 we render a small
       health probe that proves the WebView loaded and reached the sidecar. -->
  <DeckPlaceholder {status} {discovery} {health} error={errorMsg} />
</main>

<style>
  :global(html, body) {
    margin: 0;
    background: #0d1117;
  }
  main {
    width: 100vw;
    min-height: 100vh;
    box-sizing: border-box;
  }
</style>
