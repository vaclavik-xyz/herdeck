<script lang="ts">
  // The real deck (slice 2): polls the sidecar's /state, refetches only the
  // tiles/panel whose version advanced, and turns clicks into POST /press — the
  // proven loop from src/herdeck/driver/web.py _PAGE, ported to Svelte over the
  // framework-free helpers in deckClient.ts (which carry all the tested logic).
  import { onMount, untrack } from "svelte";
  import {
    DeckDiffer,
    LONG_POLL_MS,
    OfflineDebounce,
    nextPollDelay,
    stepDeck,
    initialView,
    summaryLabel,
    type DeckTransport,
    type DeckViewModel,
    type PressResult,
  } from "./deckClient";
  import { defineMessages, fmt, locale, setLang } from "./i18n.svelte";
  import { visibilityGatedLoop, type GatedLoop } from "./pollGate";

  const M = defineMessages({
    en: {
      tile: "tile {n}",
      panel: "status panel",
      offline_title: "Waiting for the runtime",
      offline_body: "The deck appears here as soon as the local Herdeck runtime answers.",
      status_offline: "offline · reconnecting…",
      status_mock: "mock",
      status_live: "live",
      status_disconnected: "live · disconnected",
      press_failed: "Press didn't reach the runtime",
      press_forbidden: "Press refused: the runtime's access token changed",
      press_rejected: "The runtime rejected the press (HTTP {status})",
    },
    cs: {
      tile: "dlaždice {n}",
      panel: "stavový panel",
      offline_title: "Čekám na runtime",
      offline_body: "Deck se zobrazí, jakmile odpoví lokální Herdeck runtime.",
      status_offline: "offline · připojuji znovu…",
      status_mock: "mock",
      status_live: "live",
      status_disconnected: "live · odpojeno",
      press_failed: "Stisk se nedostal k runtime",
      press_forbidden: "Stisk odmítnut: změnil se přístupový token runtime",
      press_rejected: "Runtime stisk odmítl (HTTP {status})",
    },
  });
  const m = $derived(M[locale.lang]);

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
    // Retry / fallback interval. Normally the deck long-polls /state (the
    // runtime answers the moment its version moves); this cadence applies
    // while failing, while an image retry is pending, and against a runtime
    // that ignores the long-poll params.
    pollMs?: number;
    onJump?: (section: string) => void;
    onView?: (view: DeckViewModel) => void;
    compact?: boolean;
  } = $props();

  let view = $state<DeckViewModel>(initialView());
  let active = $state<number | null>(null); // last-pressed cell, for the outline
  let differ = new DeckDiffer();
  let offline = new OfflineDebounce();
  let loop: GatedLoop | null = null; // the poll loop handle (kick after a press)
  let nextDelay = 0; // set by each step: when the loop runs the next one

  async function step(): Promise<void> {
    const t = transport;
    const d = differ;
    if (!t) {
      nextDelay = pollMs;
      if (view.online) view = { ...view, online: false };
      onView?.(view);
      return;
    }
    const before = d.syncedVersion;
    const longPolled = before >= 0;
    const started = Date.now();
    const next = await stepDeck(t, d, view, {
      waitMs: longPolled ? LONG_POLL_MS : 0,
      offline,
    });
    // A long-poll can outlive its transport (sidecar restart) or the component;
    // its answer belongs to the old runtime's version space — drop it.
    if (!alive || t !== transport || d !== differ) {
      nextDelay = 0;
      return;
    }
    nextDelay = nextPollDelay({
      pollMs,
      failing: offline.failing,
      longPolled,
      before,
      after: d.syncedVersion,
      elapsedMs: Date.now() - started,
    });
    view = next;
    onView?.(view);
    // The deck's [view].language leads; the window follows so tiles and chrome
    // always speak the same language. Only a real /state carries it: an offline
    // model's `language` is initialView's "en" placeholder, and applying it
    // would override the editor's configured language until the runtime answers.
    if (view.online) setLang(view.language);
  }

  // Scheme image failed to load (an older shell without the `herdeck` URI
  // scheme, or a transient miss): switch the transport to its base64 command
  // path and swap in the replacement, unless a newer frame already replaced it.
  async function imageFailed(which: number | "panel", failedSrc: string): Promise<void> {
    const t = transport;
    const fb = t?.imageFallback;
    if (!fb || failedSrc.startsWith("data:")) return;
    let src: string | null;
    try {
      src = which === "panel" ? await fb.panel() : await fb.tile(which);
    } catch {
      return; // the next version's poll retries through the fallback path
    }
    if (!alive || t !== transport) return;
    if (which === "panel") {
      if (view.panel === failedSrc) view = { ...view, panel: src };
      return;
    }
    if (view.tiles[which] !== failedSrc) return;
    const tiles = { ...view.tiles };
    if (src) tiles[which] = src;
    else delete tiles[which];
    view = { ...view, tiles };
  }

  // One press path for clicks and keys: POST the press, outline the cell. The
  // panel uses index === slots (no button), matching web.py's press(slotCount).
  async function press(i: number): Promise<void> {
    if (!transport) return;
    let r: PressResult | null;
    try {
      r = await transport.press(i);
    } catch {
      r = null; // network / proxy failure (e.g. the runtime moved ports)
    }
    // The component can be torn down (window-mode switch, quit) while the POST
    // is in flight; without this the resolving press installs a timer that
    // teardown has already run past, and writes state on a dead component.
    if (!alive) return;
    if (!r || !r.ok) {
      // A press that did nothing used to be silent — indistinguishable from a
      // deck that ignored the click. Say so, briefly, on the cell itself.
      flashFailure(
        i,
        !r
          ? m.press_failed
          : r.forbidden
            ? m.press_forbidden
            : fmt(m.press_rejected, { status: r.status }),
      );
      return;
    }
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
    clearFailure();
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
    clearFailure();
  }

  // Failed-press feedback: the cell gets a red ring + title, and a short
  // message (role=status) says why. Cleared after FAILED_MS or on the next press.
  const FAILED_MS = 2500;
  let failed = $state<{ index: number; message: string } | null>(null);
  let failedTimer: ReturnType<typeof setTimeout> | undefined;

  function flashFailure(i: number, message: string): void {
    if (failedTimer) clearTimeout(failedTimer);
    if (activeTimer) clearTimeout(activeTimer);
    activeTimer = undefined;
    active = null;
    failed = { index: i, message };
    failedTimer = setTimeout(() => {
      failedTimer = undefined;
      failed = null;
    }, FAILED_MS);
  }

  function clearFailure(): void {
    if (failedTimer) clearTimeout(failedTimer);
    failedTimer = undefined;
    failed = null;
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
        offline = new OfflineDebounce();
        view = initialView(view.slots);
        clearActive();
        // Don't wait out the old transport's poll (a long-poll can hold for
        // LONG_POLL_MS): run a step on the new one as soon as it returns.
        loop?.kick();
      });
    }
  });

  onMount(() => {
    // Visibility-gated self-scheduling loop (web.py's pattern + tray-app gating):
    // the next poll is scheduled only AFTER the current step resolves (steps
    // never overlap), and the loop parks entirely while the window is hidden —
    // the deck lives in the tray, so hidden webviews must not keep polling and
    // refetching tile PNGs nobody sees. One immediate step fires on show.
    // Each step picks the next delay: 0 to re-arm a long-poll, `pollMs` when
    // retrying or when the runtime does not hold long-polls.
    loop = visibilityGatedLoop(step, () => nextDelay);
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
  // `online` is already debounced (OfflineDebounce), so this footer text — and
  // with it the aria-live announcement — only changes on a real state change,
  // not on every transient poll failure.
  const statusText = $derived(
    !view.online
      ? m.status_offline
      : view.source === "mock"
        ? m.status_mock
        : view.connected
          ? m.status_live
          : m.status_disconnected,
  );
</script>

<section class="deck" class:offline={!view.online} class:compact>
  <div class="stage">
  <div class="grid">
    {#each cells as i (i)}
      {@const src = view.tiles[i]}
      <button
        class="cell"
        class:active={active === i}
        class:alt={pressParity}
        class:failed={failed?.index === i}
        title={failed?.index === i ? failed.message : undefined}
        onclick={() => clickTile(i)}
        aria-label={view.labels[i] ?? fmt(m.tile, { n: i + 1 })}
      >
        {#if src}<img {src} alt="" onerror={() => void imageFailed(i, src)} />{/if}
      </button>
    {/each}
    <button
      class="panel"
      class:active={active === view.slots}
      class:alt={pressParity}
      class:failed={failed?.index === view.slots}
      title={failed?.index === view.slots ? failed.message : undefined}
      onclick={() => { if (!onJump) void press(view.slots); }}
      aria-label={m.panel}
    >
      {#if view.panel}{@const psrc = view.panel}<img
          src={psrc}
          alt=""
          onerror={() => void imageFailed("panel", psrc)}
        />{/if}
    </button>
  </div>
  {#if !view.online}
    <div class="deck-offline" class:mini={compact}>
      <strong>{m.offline_title}</strong>
      {#if !compact}
        <p>{m.offline_body}</p>
      {/if}
    </div>
  {/if}
  {#if failed}
    <div class="press-error" role="status">{failed.message}</div>
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
    /* `online` is debounced (3 failed polls or ~1s), but a flaky runtime can
       still raise this over a deck that actuates between failures. The pill is
       informational; it must not swallow those presses (the desktop card,
       which offers a full explanation instead of a live deck, still may). */
    pointer-events: none;
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
  /* Panel pins to the last two cells of the bottom row — same placement as the
     web simulator. `position: relative` is load-bearing: it makes this the
     containing block for the out-of-flow image below. */
  .panel {
    grid-column: 4 / 6;
    position: relative;
  }
  /* Failed press: a red ring that holds for the message's lifetime. Declared
     before .active so a successful re-press (which clears `failed`) wins. */
  .cell.failed,
  .panel.failed {
    outline: 2px solid var(--st-blocked);
    outline-offset: -2px;
  }
  .press-error {
    position: absolute;
    left: 50%;
    bottom: 6px;
    transform: translateX(-50%);
    max-width: calc(100% - 16px);
    padding: 4px 10px;
    border: 1px solid var(--st-blocked);
    border-radius: 999px;
    background: var(--panel-raised);
    color: var(--text);
    font: var(--t-label);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    pointer-events: none;
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
  /* The row height must come from the square tiles, never from the panel. The
     panel spans two columns PLUS the gap between them, so its 2:1 artwork
     (392x196, against 196x196 tiles) wants gap/2 more height than a tile. Left
     in flow, `height: 100%` degenerates to auto in an auto-height grid row, so
     the image sized itself, dragged the row with it, and the panel hung a few
     pixels below the tiles beside it. Out of flow it cannot; `contain`
     letterboxes into the extra gap width instead of stretching the art.

     This hands the row height to the panel's NEIGHBOURS, so it assumes it has
     some — true only while the deck is five columns wide, since the sidecar
     reports `slots = cols * rows - 2` and the last row keeps a 3-tile
     remainder. A wider `[deck].grid` (say 8x4 -> 30 slots) fills whole rows
     against the `repeat(5, 1fr)` above, leaves the panel alone in a row with no
     in-flow content, and that row collapses to zero. Such a deck already
     renders wrong here — the column count is hardcoded and the tiles wrap at
     five whatever the hardware says — but note that this rule turns "the panel
     sits wrong" into "the panel is gone". Giving it back a height of its own
     (`aspect-ratio`) is not the fix: a definite width would make it contribute
     (2C + gap) / 2 to row sizing again and restore the overhang. */
  .panel img {
    position: absolute;
    inset: 0;
    object-fit: contain;
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
