<script lang="ts">
  import { onMount } from "svelte";
  import { invoke } from "@tauri-apps/api/core";
  import { listen } from "@tauri-apps/api/event";
  import DeckView from "./lib/DeckView.svelte";
  import Onboarding from "./lib/Onboarding.svelte";
  import { asDiscovery, type Discovery } from "./lib/sidecar";
  import { commandTransport } from "./lib/deckClient";
  import {
    setupTransport,
    shouldOnboard,
    type SetupStatus,
  } from "./lib/onboardingClient";

  let discovery = $state<Discovery | null>(null);
  let status = $state<SetupStatus | null>(null);
  // Manual "change connection" override: open the welcome card even when the
  // status would show the deck (so a demo/local-pinned user can re-onboard).
  let reonboard = $state(false);

  // The deck reaches the sidecar through token-free Tauri proxies; the setup
  // transport uses the two token-injecting setup commands. Both need discovery
  // first (the Rust commands resolve the sidecar from it).
  const transport = $derived(
    discovery ? commandTransport((cmd, args) => invoke(cmd, args)) : null,
  );
  const setup = $derived(
    discovery ? setupTransport((cmd, args) => invoke(cmd, args)) : null,
  );

  // Which surface to show. Defaults to the deck so no setup state traps the user.
  const view = $derived(shouldOnboard(status, reonboard));

  async function pullDiscovery(): Promise<void> {
    try {
      const d = asDiscovery(await invoke("get_discovery"));
      if (d) discovery = d;
    } catch {
      // Not in a Tauri WebView (plain browser): leave null, DeckView goes offline.
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

    // Poll /setup once discovery is up. A few seconds is enough: after a
    // successful connect (and once the source swap settles) the next poll flips
    // the card to the deck without a manual refresh.
    void (async () => {
      while (alive) {
        if (setup) status = await setup.status();
        await new Promise((r) => setTimeout(r, status ? 2500 : 600));
      }
    })();

    return () => {
      alive = false;
    };
  });

  function onConnected(): void {
    reonboard = false;
    // Re-poll promptly so the card flips as soon as the swap settles.
    void (async () => {
      if (setup) status = await setup.status();
    })();
  }
</script>

<main>
  {#if view === "deck"}
    <DeckView {transport} />
    <!-- Re-onboarding affordance: reachable beyond first run, so a user pinned by
         a demo/local marker can switch connection (the backend /setup/connect is
         not first-run-gated). -->
    <button
      class="reonboard"
      title="Změnit připojení"
      aria-label="Změnit připojení"
      onclick={() => (reonboard = true)}>⚙</button
    >
  {:else}
    <Onboarding
      {view}
      {status}
      transport={setup}
      {onConnected}
      onDismiss={reonboard ? () => (reonboard = false) : undefined}
    />
  {/if}
</main>

<style>
  :global(html, body) {
    margin: 0;
    background: #0b0b0d;
  }
  main {
    position: relative;
    width: 100vw;
    min-height: 100vh;
    box-sizing: border-box;
  }
  .reonboard {
    position: fixed;
    left: 8px;
    bottom: 8px;
    width: 22px;
    height: 22px;
    padding: 0;
    border: none;
    border-radius: 6px;
    background: #17171b;
    color: #8b97a4;
    font-size: 12px;
    line-height: 22px;
    cursor: pointer;
    opacity: 0.55;
  }
  .reonboard:hover {
    opacity: 1;
  }
</style>
