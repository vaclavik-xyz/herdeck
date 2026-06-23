<script lang="ts">
  import { onMount } from "svelte";
  import { invoke } from "@tauri-apps/api/core";
  import { listen } from "@tauri-apps/api/event";
  import DeckView from "./lib/DeckView.svelte";
  import { asDiscovery, sidecarTransport, type Discovery } from "./lib/sidecar";

  let discovery = $state<Discovery | null>(null);

  // The DeckView talks to the sidecar directly with the url + token the shell
  // reports (built once here, reused — see sidecar.ts). Null until both are
  // available; DeckView then shows its offline state.
  const transport = $derived(sidecarTransport(discovery));

  // Ask the Rust shell for the sidecar discovery. The shell stores the latest
  // discovery and emits a "discovery" event whenever the supervised sidecar
  // (re)starts; we both poll once and subscribe.
  async function pullDiscovery(): Promise<void> {
    try {
      const d = asDiscovery(await invoke("get_discovery"));
      if (d) discovery = d;
    } catch {
      // invoke is unavailable outside the Tauri WebView (e.g. a plain browser);
      // leave discovery null — DeckView stays offline.
    }
  }

  onMount(() => {
    let alive = true;

    void listen<Discovery>("discovery", (event) => {
      const d = asDiscovery(event.payload);
      if (d) discovery = d;
    });

    // Retry discovery until the supervised sidecar has printed its first line.
    void (async () => {
      while (alive && !discovery) {
        await pullDiscovery();
        if (!discovery) await new Promise((r) => setTimeout(r, 400));
      }
    })();

    return () => {
      alive = false;
    };
  });
</script>

<main>
  <DeckView {transport} />
</main>

<style>
  :global(html, body) {
    margin: 0;
    background: #0b0b0d;
  }
  main {
    width: 100vw;
    min-height: 100vh;
    box-sizing: border-box;
  }
</style>
