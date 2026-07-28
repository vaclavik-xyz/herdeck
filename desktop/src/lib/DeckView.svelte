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
  import { locale, setLang } from "./i18n.svelte";
  import { visibilityGatedLoop, type GatedLoop } from "./pollGate";

  let {
    transport,
    pollMs = 300,
    onJump = undefined,
    onView = undefined,
    compact = false,
  }: {
    // Live transport (built from the sidecar url + token via sidecar.ts). Null
    // until the shell reports both; the deck then renders its offline state.
    transport: DeckTransport | null;
    pollMs?: number;
    onJump?: (section: string) => void;
    onView?: (view: DeckViewModel) => void;
    compact?: boolean;
  } = $props();

  let view = $state<DeckViewModel>(initialView());
  let active = $state<number | null>(null); // last-pressed cell, for the outline
  let differ = new DeckDiffer();
  let loop: GatedLoop | null = null; // the poll loop handle (kick after a press)

  async function step(): Promise<void> {
    if (!transport) {
      view = { ...view, online: false };
      onView?.(view);
      return;
    }
    view = await stepDeck(transport, differ, view);
    onView?.(view);
    // The deck's [view].language leads; the window follows so tiles and chrome
    // always speak the same language.
    setLang(view.language);
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
    // The component can be torn down (window-mode switch, quit) while the POST
    // is in flight; without this the resolving press installs a timer that
    // teardown has already run past, and writes state on a dead component.
    if (!r.ok || !alive) return;
    flashActive(i);
    // The sidecar re-renders synchronously inside the POST handler, so the
    // updated frame already exists — show it now instead of waiting out the
    // 300ms poll (up to half a second of dead time on the primary interaction).
    loop?.kick();
  }

  // The outline is press FEEDBACK, not a selection: it says "that press landed".
  // It used to be set and never cleared, so the last-pressed cell kept a blue
  // ring forever — and because it is keyed by slot index, the ring stayed put
  // while the agent under it changed, marking an unrelated tile.
  const ACTIVE_MS = 450;
  let activeTimer: ReturnType<typeof setTimeout> | undefined;
  let alive = true;
  // Re-pressing the SAME cell inside the flash window would otherwise just push
  // the deadline out with the ring already lit — indistinguishable from a press
  // that never landed. The parity flips per press and swaps the animation NAME,
  // which restarts the animation synchronously (no rAF, so it stays testable).
  let pressParity = $state(false);

  function flashActive(i: number): void {
    if (activeTimer) clearTimeout(activeTimer);
    active = i;
    pressParity = !pressParity;
    activeTimer = setTimeout(() => {
      activeTimer = undefined;
      active = null;
    }, ACTIVE_MS);
  }

  function clearActive(): void {
    if (activeTimer) clearTimeout(activeTimer);
    activeTimer = undefined;
    active = null;
  }

  // Config-window preview passes onJump → "jump mode": a tile click switches the editor
  // to that tile's config section and NEVER actuates the deck. The floating deck leaves
  // onJump undefined and keeps the press behavior below.
  function clickTile(i: number): void {
    if (onJump) {
      const section = view.sections[i];
      if (section) onJump(section);
      return;
    }
    void press(i);
  }

  // Keyboard parity with the simulator: 1..9 -> tiles 0..8, 0 -> tile 9.
  function onKey(e: KeyboardEvent): void {
    if (onJump) return; // jump-mode preview never actuates via keyboard
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
        clearActive();
      });
    }
  });

  onMount(() => {
    // Visibility-gated self-scheduling loop (web.py's pattern + tray-app gating):
    // the next poll is scheduled only AFTER the current step resolves (steps
    // never overlap), and the loop parks entirely while the window is hidden —
    // the deck lives in the tray, so hidden webviews must not keep polling and
    // refetching tile PNGs nobody sees. One immediate step fires on show.
    loop = visibilityGatedLoop(step, () => pollMs);
    window.addEventListener("keydown", onKey);
    return () => {
      alive = false;
      loop?.stop();
      loop = null;
      clearActive();
      window.removeEventListener("keydown", onKey);
    };
  });

  const cells = $derived(Array.from({ length: view.slots }, (_, i) => i));
  const offlineTitle = $derived(
    locale.lang === "cs" ? "Čekám na runtime" : "Waiting for the runtime",
  );
  const statusText = $derived(
    !view.online
      ? locale.lang === "cs"
        ? "offline · připojuji znovu…"
        : "offline · reconnecting…"
      : view.source === "mock"
        ? "mock"
        : view.connected
          ? "live"
          : locale.lang === "cs"
            ? "live · odpojeno"
            : "live · disconnected",
  );
</script>

<section class="deck" class:offline={!view.online} class:compact>
  <div class="stage">
  <div class="grid">
    {#each cells as i (i)}
      <button
        class="cell"
        class:active={active === i}
        class:alt={pressParity}
        onclick={() => clickTile(i)}
        aria-label={locale.lang === "cs" ? `dlaždice ${i + 1}` : `tile ${i + 1}`}
      >
        {#if view.tiles[i]}<img src={view.tiles[i]} alt="" />{/if}
      </button>
    {/each}
    <button
      class="panel"
      class:active={active === view.slots}
      class:alt={pressParity}
      onclick={() => { if (!onJump) void press(view.slots); }}
      aria-label={locale.lang === "cs" ? "stavový panel" : "status panel"}
    >
      {#if view.panel}<img src={view.panel} alt="" />{/if}
    </button>
  </div>
  {#if !view.online}
    <div class="deck-offline" class:mini={compact}>
      <strong>{offlineTitle}</strong>
      {#if !compact}
        <p>{locale.lang === "cs"
          ? "Deck se zobrazí, jakmile odpoví lokální Herdeck runtime."
          : "The deck appears here as soon as the local Herdeck runtime answers."}</p>
      {/if}
    </div>
  {/if}
  </div>

  <footer class="summary" aria-live="polite">
    <span
      class="dot"
      class:on={view.online && (view.source !== "live" || view.connected)}
      class:mock={view.online && view.source === "mock"}
      class:warn={view.online && view.summary.blocked > 0}
    ></span>
    <span class="counts">{summaryLabel(view.summary, locale.lang)}</span>
    <span class="src">{statusText}</span>
  </footer>
</section>

<style>
  .stage { position: relative; }
  .deck-offline {
    position: absolute;
    inset: 0;
    display: grid;
    align-content: center;
    justify-items: center;
    gap: var(--s1);
    padding: var(--s5);
    border-radius: var(--r-panel);
    background: color-mix(in srgb, var(--canvas) 78%, transparent);
    text-align: center;
  }
  .deck-offline strong { font: var(--t-h2); color: var(--text); }
  .deck-offline p { margin: 0; max-width: 34ch; color: var(--text-dim); font: var(--t-help); }
  /* The compact deck hides its footer from sight (sr-only), so an unreachable
     runtime used to render as 13 blank keys with nothing saying why — the same
     picture as a deck that simply has no agents. Same overlay, sized for a
     328px card: one pill, no paragraph. */
  .deck-offline.mini {
    padding: var(--s2);
    background: color-mix(in srgb, var(--canvas) 62%, transparent);
  }
  .deck-offline.mini strong {
    padding: 4px 10px;
    border: 1px solid var(--line-strong);
    border-radius: 999px;
    background: var(--panel-raised);
    font: var(--t-label);
    color: var(--text-dim);
  }
  .deck {
    display: flex;
    flex-direction: column;
    gap: 8px;
    box-sizing: border-box;
    padding: 10px;
    background: var(--canvas);
    font: 12px/1.3 system-ui, -apple-system, sans-serif;
    color: var(--text);
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 6px;
    padding: 10px;
    border-radius: 11px;
    background: var(--key);
  }
  .cell,
  .panel {
    border: none;
    padding: 0;
    border-radius: 8px;
    background: var(--panel);
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
    outline: 2px solid var(--accent-strong);
    outline-offset: -2px;
    animation: press-a var(--dur) var(--ease);
  }
  /* Same keyframes under a second name: flipping the class on a re-press swaps
     the animation-name, which is what restarts the animation. */
  .cell.active.alt,
  .panel.active.alt {
    animation-name: press-b;
  }
  @keyframes press-a { from { outline-color: var(--text); } }
  @keyframes press-b { from { outline-color: var(--text); } }
  /* theme.css flattens every animation to .01ms under reduced motion, which
     would leave those users with the very "did that press land?" ambiguity this
     parity exists to remove. Give them a STATIC difference instead: consecutive
     presses alternate the ring colour, which no animation rule can flatten. */
  @media (prefers-reduced-motion: reduce) {
    .cell.active.alt,
    .panel.active.alt {
      outline-color: var(--text);
    }
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
    background: var(--st-unknown);
  }
  .dot.on {
    background: var(--st-working);
  }
  .dot.mock {
    background: var(--st-blocked);
  }
  .dot.warn {
    background: var(--st-waiting);
  }
  .counts {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .src {
    color: var(--text-dim);
    font-size: 11px;
    white-space: nowrap;
  }
  .deck.compact {
    gap: 0;
    padding: 8px;
    background: var(--canvas);
  }
  .deck.compact .grid {
    gap: 4px;
    padding: 0;
    border-radius: 0;
    background: transparent;
  }
  .deck.compact .cell,
  .deck.compact .panel {
    border-radius: 7px;
    background: var(--panel);
    box-shadow: inset 0 0 0 1px var(--line);
  }
  .deck.compact footer.summary {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
</style>
