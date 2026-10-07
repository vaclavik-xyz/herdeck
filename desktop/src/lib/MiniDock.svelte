<script lang="ts">
  // The mini dock: the floating window's default content ([desktop].floating
  // = "dock"). A narrow strip of the agents that are not idle, idle ones
  // folded into "+N", one card on hover (after an intent delay), keyboard
  // focus or click (click pins it until Esc / a click elsewhere). Data:
  // /agent/list long-polled via the Rust `agent_call` proxy (dockClient.ts);
  // actions: the agent card routes (agentCardClient.ts). While a card is open
  // the window grows beside the strip through Rust's `dock_layout`, which
  // keeps the strip where it is on screen.
  import { untrack } from "svelte";
  import DockItem from "./DockItem.svelte";
  import DockCard from "./DockCard.svelte";
  import { visibilityGatedLoop } from "./pollGate";
  import {
    DOCK_POLL_MS,
    DOCK_RETRY_MS,
    dockNextDelay,
    type DockAgent,
    type DockList,
    type DockTransport,
  } from "./dockClient";
  import type { AgentTransport } from "./agentCardClient";
  import {
    agentKey,
    CARD_GAP,
    CARD_MAX_HEIGHT,
    CARD_WIDTH,
    dockLayout,
    HOVER_CLOSE_MS,
    HOVER_OPEN_MS,
    liveSince,
    neutralReason,
    STRIP_WIDTH,
    type DockPlacement,
    type WindowLayoutFn,
  } from "./dockModel";
  import { DOCK_MESSAGES } from "./dockMessages";
  import { fmt, locale, setLang } from "./i18n.svelte";

  type Target = { kind: "agent"; key: string } | { kind: "idle" };

  function prefersReducedMotion(): boolean {
    try {
      return typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch {
      return false;
    }
  }

  let {
    transport,
    agentTransport,
    windowLayout,
    startDrag = () => {},
    onOpenSettings = () => {},
    reducedMotion = prefersReducedMotion(),
    hoverOpenMs = HOVER_OPEN_MS,
    hoverCloseMs = HOVER_CLOSE_MS,
  }: {
    transport: DockTransport | null;
    agentTransport: AgentTransport | null;
    windowLayout: WindowLayoutFn;
    startDrag?: (event: PointerEvent) => void;
    onOpenSettings?: () => void;
    reducedMotion?: boolean;
    hoverOpenMs?: number;
    hoverCloseMs?: number;
  } = $props();

  const m = $derived(DOCK_MESSAGES[locale.lang]);

  let list = $state<DockList | null>(null);
  let stale = $state(false);
  let receivedAt = $state(Date.now());
  let now = $state(Date.now());
  let open = $state<Target | null>(null);
  let pinned = $state(false);
  let revealed = $state(false);
  let anchorTop = $state(0);
  let placement = $state<DockPlacement>({ side: "right", stripX: 0, stripY: 0 });
  let stripHeight = $state(0);
  let sentKey = $state<string | null>(null);
  let stripEl = $state<HTMLElement | undefined>(undefined);
  let cardEl = $state<HTMLElement | undefined>(undefined);
  // Read only inside timers, never rendered: plain flags.
  let hoverCard = false;
  let hoverItem = false;

  const layout = $derived(dockLayout(list?.agents ?? []));
  const neutral = $derived(neutralReason(list));
  const neutralTitle = $derived(
    neutral === "empty"
      ? m.neutral_empty
      : neutral === "disconnected"
        ? m.neutral_disconnected
        : m.neutral_loading,
  );
  const openAgent = $derived(findAgent(open, list));
  const cardLeft = $derived(
    placement.side === "right"
      ? placement.stripX + STRIP_WIDTH + CARD_GAP
      : placement.stripX - CARD_GAP - CARD_WIDTH,
  );

  function findAgent(target: Target | null, current: DockList | null): DockAgent | null {
    if (target === null || target.kind !== "agent" || current === null) return null;
    return current.agents.find((a) => agentKey(a) === target.key) ?? null;
  }

  function iconFor(a: DockAgent): string | null {
    return a.icon && transport ? transport.iconUrl(a.icon) : null;
  }

  // --- data: /agent/list, long-polled while the window is visible ---------
  let version: number | null = null;
  let nextDelay = 0;

  async function step(): Promise<void> {
    const t = transport;
    if (!t) {
      nextDelay = DOCK_RETRY_MS;
      return;
    }
    const longPolled = version !== null;
    const started = Date.now();
    const got = await t.list(version, DOCK_POLL_MS);
    const elapsedMs = Date.now() - started;
    if (got === null) {
      // Keep the last list (dimmed); retry on the slow cadence.
      stale = true;
      nextDelay = dockNextDelay({ failed: true, longPolled, changed: false, elapsedMs });
      return;
    }
    const changed = got.version !== version;
    stale = false;
    if (changed || list === null) {
      list = got;
      receivedAt = Date.now();
      setLang(got.language);
    }
    version = got.version;
    nextDelay = dockNextDelay({ failed: false, longPolled, changed, elapsedMs });
  }

  $effect(() => {
    if (!transport) return;
    return untrack(() => {
      version = null;
      const loop = visibilityGatedLoop(step, () => nextDelay);
      return () => loop.stop();
    });
  });

  // Elapsed times tick only while a card shows them.
  $effect(() => {
    if (open === null) return;
    now = Date.now();
    const id = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(id);
  });

  // --- window size: Rust dock_layout keeps the strip still on screen --------
  let layoutSeq = 0;
  async function applyLayout(expanded: boolean): Promise<void> {
    const seq = ++layoutSeq;
    let next: DockPlacement;
    try {
      next = await windowLayout({
        stripWidth: STRIP_WIDTH,
        stripHeight: Math.max(stripHeight, STRIP_WIDTH),
        cardWidth: expanded ? CARD_WIDTH : 0,
        cardHeight: expanded ? anchorTop + CARD_MAX_HEIGHT : 0,
      });
    } catch {
      next = { side: "right", stripX: 0, stripY: 0 }; // plain browser preview
    }
    if (seq === layoutSeq) placement = next;
  }

  $effect(() => {
    const el = stripEl;
    if (!el) return;
    stripHeight = el.offsetHeight;
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => (stripHeight = el.offsetHeight));
    ro.observe(el);
    return () => ro.disconnect();
  });

  $effect(() => {
    void stripHeight; // the strip grew or shrank: refit the window
    untrack(() => void applyLayout(open !== null));
  });

  // --- open / close ----------------------------------------------------------
  let openTimer: ReturnType<typeof setTimeout> | undefined;
  let closeTimer: ReturnType<typeof setTimeout> | undefined;

  function clearTimers(): void {
    clearTimeout(openTimer);
    clearTimeout(closeTimer);
    openTimer = undefined;
    closeTimer = undefined;
  }

  function sameTarget(a: Target | null, b: Target): boolean {
    if (a === null || a.kind !== b.kind) return false;
    return a.kind === "idle" || (b.kind === "agent" && a.key === b.key);
  }

  async function show(target: Target, top: number, pin: boolean): Promise<void> {
    clearTimers();
    const wasOpen = open !== null;
    pinned = pin || (sameTarget(open, target) && pinned);
    anchorTop = top;
    open = target;
    if (!wasOpen) revealed = false;
    await applyLayout(true);
    if (open !== null) revealed = true;
  }

  async function hide(): Promise<void> {
    clearTimers();
    hoverCard = false;
    if (open === null) return;
    open = null;
    pinned = false;
    revealed = false;
    sentKey = null;
    await applyLayout(false);
  }

  function scheduleClose(): void {
    clearTimeout(closeTimer);
    closeTimer = setTimeout(() => {
      if (pinned || hoverCard || hoverItem) return;
      if (cardEl?.contains(document.activeElement)) return;
      void hide();
    }, hoverCloseMs);
  }

  function enter(target: Target, top: number): void {
    hoverItem = true;
    clearTimeout(closeTimer);
    if (pinned) return; // a pinned card stays on its agent
    clearTimeout(openTimer);
    if (open !== null) {
      void show(target, top, false);
      return;
    }
    openTimer = setTimeout(() => void show(target, top, false), hoverOpenMs);
  }

  function leave(): void {
    hoverItem = false;
    clearTimeout(openTimer);
    if (!pinned) scheduleClose();
  }

  function focusItem(target: Target, top: number): void {
    if (!pinned) void show(target, top, false);
  }

  function blurItem(): void {
    if (!pinned) scheduleClose();
  }

  function clickItem(target: Target, top: number): void {
    void show(target, top, true);
  }

  function cardEnter(): void {
    hoverCard = true;
    clearTimeout(closeTimer);
  }

  function cardLeave(): void {
    hoverCard = false;
    if (!pinned) scheduleClose();
  }

  function markSent(): void {
    if (openAgent) sentKey = agentKey(openAgent);
  }

  function onKey(event: KeyboardEvent): void {
    if (event.key !== "Escape" || open === null) return;
    event.preventDefault();
    (document.activeElement as HTMLElement | null)?.blur?.();
    void hide();
  }

  function onBackdrop(event: MouseEvent): void {
    // A click on the transparent part of the grown window = outside the card.
    if (event.target === event.currentTarget) void hide();
  }

  $effect(() => {
    const onVisibility = () => {
      if (document.hidden) void hide();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  });

  // Close when the card has nothing left to show.
  $effect(() => {
    const a = openAgent;
    const kind = open?.kind;
    if (kind === "agent" && list !== null && a === null) {
      untrack(() => void hide()); // the agent left the fleet
    } else if (a !== null && sentKey === agentKey(a) && a.status !== "blocked") {
      untrack(() => void hide()); // answered, and it moved on
    } else if (kind === "idle" && layout.idle.length === 0) {
      untrack(() => void hide());
    }
  });

  $effect(() => () => clearTimers());
</script>

<svelte:window onkeydown={onKey} onblur={() => void hide()} />

<div class="mini-dock" class:reduced={reducedMotion} class:stale role="presentation" onclick={onBackdrop}>
  <nav
    class="strip"
    aria-label={m.strip_label}
    bind:this={stripEl}
    style:left="{placement.stripX}px"
    style:top="{placement.stripY}px"
  >
    <div
      class="grip"
      title={m.grip_title}
      aria-label={m.grip_title}
      role="presentation"
      data-tauri-drag-region
      onpointerdown={startDrag}
    >
      <span class="grip-bar" data-tauri-drag-region></span>
    </div>
    {#if neutral !== null}
      <button type="button" class="neutral" title={neutralTitle} aria-label={neutralTitle} onclick={onOpenSettings}>
        <span class="neutral-dot" aria-hidden="true"></span>
      </button>
    {:else}
      {#each layout.visible as agent (agentKey(agent))}
        <DockItem
          {agent}
          iconSrc={iconFor(agent)}
          active={open?.kind === "agent" && open.key === agentKey(agent)}
          onenter={(top) => enter({ kind: "agent", key: agentKey(agent) }, top)}
          onleave={leave}
          onfocus={(top) => focusItem({ kind: "agent", key: agentKey(agent) }, top)}
          onblur={blurItem}
          onclick={(top) => clickItem({ kind: "agent", key: agentKey(agent) }, top)}
        />
      {/each}
      {#if layout.idle.length > 0}
        <button
          type="button"
          class="idle-chip"
          class:active={open?.kind === "idle"}
          title={fmt(m.idle_title, { n: layout.idle.length })}
          aria-label={fmt(m.idle_title, { n: layout.idle.length })}
          onmouseenter={(e) => enter({ kind: "idle" }, e.currentTarget.offsetTop)}
          onmouseleave={leave}
          onfocus={(e) => focusItem({ kind: "idle" }, e.currentTarget.offsetTop)}
          onblur={blurItem}
          onclick={(e) => clickItem({ kind: "idle" }, e.currentTarget.offsetTop)}
        >+{layout.idle.length}</button>
      {/if}
    {/if}
    {#if stale}
      <span class="offline-dot" role="img" title={m.stale_title} aria-label={m.stale_title}></span>
    {/if}
  </nav>
  {#if open !== null && revealed}
    <div
      class="card-slot"
      bind:this={cardEl}
      role="presentation"
      style:left="{cardLeft}px"
      style:top="{placement.stripY + anchorTop}px"
      onmouseenter={cardEnter}
      onmouseleave={cardLeave}
    >
      {#if openAgent}
        {#key agentKey(openAgent)}
          <DockCard
            agent={openAgent}
            sinceOf={(a) => liveSince(a.sinceS, receivedAt, now)}
            iconUrl={iconFor}
            transport={agentTransport}
            onClose={() => void hide()}
            onSent={markSent}
          />
        {/key}
      {:else if open.kind === "idle"}
        <DockCard
          idle={layout.idle}
          sinceOf={(a) => liveSince(a.sinceS, receivedAt, now)}
          iconUrl={iconFor}
          transport={agentTransport}
          onClose={() => void hide()}
        />
      {/if}
    </div>
  {/if}
</div>

<style>
  .mini-dock {
    position: fixed;
    inset: 0;
    overflow: hidden;
    font: var(--t-body);
    color: var(--text);
  }
  .strip {
    position: absolute;
    width: 56px;
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--s1);
    padding: 0 0 var(--s2);
    background: color-mix(in srgb, var(--panel) 90%, transparent);
    border: 1px solid var(--line-strong);
    border-radius: 28px;
    backdrop-filter: blur(14px);
    transition: opacity var(--dur) var(--ease);
  }
  .stale .strip { opacity: .55; }
  .grip {
    width: 100%;
    height: 18px;
    display: grid;
    place-items: center;
    cursor: grab;
  }
  .grip-bar {
    width: 20px;
    height: 4px;
    border-radius: 2px;
    background: var(--line-strong);
  }
  .neutral, .idle-chip {
    width: 44px;
    height: 44px;
    border-radius: 50%;
    border: 1px solid var(--line-strong);
    background: var(--key);
    color: var(--text-dim);
    font: var(--t-label);
    cursor: pointer;
    flex: none;
  }
  .idle-chip.active, .idle-chip:focus-visible { box-shadow: 0 0 0 2px var(--accent-ring); outline: none; }
  .neutral-dot {
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--st-unknown);
  }
  .offline-dot {
    position: absolute;
    top: 5px;
    right: 6px;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--st-offline);
  }
  .card-slot {
    position: absolute;
    width: 300px;
    animation: dock-card-in 200ms cubic-bezier(.2, 1.3, .4, 1);
    transform-origin: left top;
  }
  @keyframes dock-card-in {
    from { opacity: 0; transform: scale(.96) translateY(-4px); }
    to { opacity: 1; transform: none; }
  }
  .reduced .card-slot { animation: none; }
  .reduced .strip { transition: none; }
  @media (prefers-reduced-motion: reduce) {
    .card-slot { animation: none; }
    .strip { transition: none; }
  }
</style>
