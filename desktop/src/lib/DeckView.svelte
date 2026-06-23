<script lang="ts">
  // The real deck (slice 2): polls the sidecar's /state, refetches only the
  // tiles/panel whose version advanced, and turns clicks into POST /press — the
  // proven loop from src/herdeck/driver/web.py _PAGE, ported to Svelte over the
  // framework-free helpers in deckClient.ts (which carry all the tested logic).
  import { onMount, untrack } from "svelte";
  import {
    DeckDiffer,
    stepDeck,
    initialView,
    summaryLabel,
    type DeckTransport,
    type DeckViewModel,
  } from "./deckClient";

  let {
    transport,
    pollMs = 300,
  }: {
    // Live transport (built from the sidecar url + token via sidecar.ts). Null
    // until the shell reports both; the deck then renders its offline state.
    transport: DeckTransport | null;
    pollMs?: number;
  } = $props();

  let view = $state<DeckViewModel>(initialView());
  let active = $state<number | null>(null); // last-pressed cell, for the outline
  let differ = new DeckDiffer();

  async function step(): Promise<void> {
    if (!transport) {
      view = { ...view, online: false };
      return;
    }
    view = await stepDeck(transport, differ, view);
  }

  // One press path for clicks and keys: POST the press, outline the cell. The
  // panel uses index === slots (no button), matching web.py's press(slotCount).
  async function press(i: number): Promise<void> {
    if (!transport) return;
    let r;
    try {
      r = await transport.press(i);
    } catch {
      return;
    }
    if (!r.ok) return;
    active = i;
  }

  // Keyboard parity with the simulator: 1..9 -> tiles 0..8, 0 -> tile 9.
  function onKey(e: KeyboardEvent): void {
    if (e.repeat || e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return;
    if (e.key >= "1" && e.key <= "9") void press(e.key.charCodeAt(0) - 49);
    else if (e.key === "0") void press(9);
  }

  // Restart the version gate whenever the transport identity changes (e.g. the
  // supervised sidecar restarted, so its version counter reset), making the next
  // poll refetch the whole deck. The effect tracks only `transport`; the reset
  // writes are untracked so polling's `view` updates don't re-trigger it.
  let lastTransport: DeckTransport | null = untrack(() => transport);
  $effect(() => {
    if (transport !== lastTransport) {
      untrack(() => {
        lastTransport = transport;
        differ = new DeckDiffer();
        view = initialView(view.slots);
        active = null;
      });
    }
  });

  onMount(() => {
    // Self-scheduling loop (web.py's pattern): schedule the next poll only AFTER
    // the current step's fetch resolves, so steps never overlap. setInterval
    // would let a slow poll finish after a newer one and move the version gate
    // backwards / clobber the view.
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function loop(): Promise<void> {
      if (stopped) return;
      await step();
      if (!stopped) timer = setTimeout(() => void loop(), pollMs);
    }
    void loop();
    window.addEventListener("keydown", onKey);
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      window.removeEventListener("keydown", onKey);
    };
  });

  const cells = $derived(Array.from({ length: view.slots }, (_, i) => i));
  const statusText = $derived(
    !view.online
      ? "offline · reconnecting…"
      : view.source === "mock"
        ? "mock"
        : view.connected
          ? "live"
          : "live · disconnected",
  );
</script>

<section class="deck" class:offline={!view.online}>
  <div class="grid">
    {#each cells as i (i)}
      <button
        class="cell"
        class:active={active === i}
        onclick={() => void press(i)}
        aria-label={`tile ${i + 1}`}
      >
        {#if view.tiles[i]}<img src={view.tiles[i]} alt="" />{/if}
      </button>
    {/each}
    <button
      class="panel"
      class:active={active === view.slots}
      onclick={() => void press(view.slots)}
      aria-label="status panel"
    >
      {#if view.panel}<img src={view.panel} alt="" />{/if}
    </button>
  </div>

  <footer class="summary">
    <span
      class="dot"
      class:on={view.online && (view.source !== "live" || view.connected)}
      class:mock={view.online && view.source === "mock"}
      class:warn={view.online && view.summary.blocked > 0}
    ></span>
    <span class="counts">{summaryLabel(view.summary)}</span>
    <span class="src">{statusText}</span>
  </footer>
</section>

<style>
  .deck {
    display: flex;
    flex-direction: column;
    gap: 8px;
    box-sizing: border-box;
    min-height: 100vh;
    padding: 10px;
    background: #0b0b0d;
    font: 12px/1.3 system-ui, -apple-system, sans-serif;
    color: #e7ecf3;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 6px;
    padding: 10px;
    border-radius: 14px;
    background: #2a2a2e;
  }
  .cell,
  .panel {
    border: none;
    padding: 0;
    border-radius: 8px;
    background: #111;
    cursor: pointer;
    overflow: hidden;
  }
  .cell {
    aspect-ratio: 1 / 1;
  }
  /* Panel pins to the last two cells of the bottom row and stretches to the
     row height the square tiles set — same placement as the web simulator. */
  .panel {
    grid-column: 4 / 6;
  }
  .cell.active,
  .panel.active {
    outline: 3px solid #5af;
    outline-offset: -3px;
  }
  .cell img,
  .panel img {
    display: block;
    width: 100%;
    height: 100%;
  }
  .deck.offline .grid {
    opacity: 0.45;
    transition: opacity 0.2s;
  }
  footer.summary {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 2px 4px;
  }
  .dot {
    flex: none;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #6b7785;
  }
  .dot.on {
    background: #3fb950;
  }
  .dot.mock {
    background: #d29922;
  }
  .dot.warn {
    background: #f0883e;
  }
  .counts {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .src {
    color: #8b97a4;
    font-size: 11px;
    white-space: nowrap;
  }
</style>
