<script lang="ts">
  // One agent on the mini dock strip: its mark inside a status ring —
  // working = open arc (spinning), waiting = dashed, done = ring + dot,
  // blocked = solid amber with a gentle pulse, offline = desaturated + slash.
  import DockMark from "./DockMark.svelte";
  import type { DockAgent } from "./dockClient";
  import { statusLabel } from "./dockMessages";
  import { locale } from "./i18n.svelte";

  let {
    agent,
    iconSrc,
    active = false,
    label: labelOverride = null,
    count = 0,
    expanded = undefined,
    onenter,
    onleave,
    onfocus,
    onblur,
    onclick,
  }: {
    agent: DockAgent;
    iconSrc: string | null;
    active?: boolean;
    /** Accessible name instead of "title · status" (a project group). */
    label?: string | null;
    /** A project group's member count, drawn as a badge (0 = none). */
    count?: number;
    /** A project group: whether its members are shown (aria-expanded). */
    expanded?: boolean;
    onenter: (top: number) => void;
    onleave: () => void;
    onfocus: (top: number) => void;
    onblur: () => void;
    onclick: (top: number) => void;
  } = $props();

  const label = $derived(labelOverride ?? `${agent.title} · ${statusLabel(locale.lang, agent.status, agent.offline)}`);
  const top = (event: Event): number => (event.currentTarget as HTMLElement).offsetTop;
</script>

<button
  type="button"
  class="item st-{agent.status}"
  class:offline={agent.offline}
  class:active
  class:group={count > 0}
  aria-label={label}
  aria-expanded={expanded}
  data-agent="{agent.serverId}/{agent.paneId}"
  onmouseenter={(e) => onenter(top(e))}
  onmouseleave={onleave}
  onfocus={(e) => onfocus(top(e))}
  onblur={onblur}
  onclick={(e) => onclick(top(e))}
>
  <span class="ring" aria-hidden="true"></span>
  {#if agent.status === "working"}
    <svg class="arc" viewBox="0 0 48 48" aria-hidden="true">
      <circle cx="24" cy="24" r="22.5" pathLength="100" />
    </svg>
  {/if}
  <span class="face"><DockMark {agent} src={iconSrc} size={30} /></span>
  {#if agent.status === "done"}<span class="done-dot" aria-hidden="true"></span>{/if}
  {#if agent.offline}<span class="slash" aria-hidden="true"></span>{/if}
  {#if count > 0}<span class="count" aria-hidden="true">{count}</span>{/if}
</button>

<style>
  .item {
    position: relative;
    width: 48px;
    height: 48px;
    display: grid;
    place-items: center;
    padding: 0;
    border: 0;
    border-radius: 50%;
    background: transparent;
    cursor: pointer;
    flex: none;
  }
  /* The mark (30 px) sits wholly inside the ring's 48 px circle: a bigger,
     square mark poked its corners through the ring and hid the spinning arc. */
  .ring {
    position: absolute;
    inset: 0;
    border-radius: 50%;
    border: 3px solid var(--st-idle);
    pointer-events: none;
  }
  .face { position: relative; display: grid; place-items: center; }
  /* The working arc is a round-capped SVG stroke on its own layer, spun with
     `transform` so the compositor turns it without repainting. A CSS border
     with one transparent side looked wobbly (its ends are mitred wedges);
     animating stroke-dashoffset instead fixed the look but repainted every
     frame and cost ~25 % of a core in WebKit's GPU and content processes. */
  .st-working .ring { border-color: color-mix(in srgb, var(--st-working) 18%, transparent); }
  .arc {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
    overflow: visible;
    animation: dock-spin 1.6s linear infinite;
    will-change: transform;
  }
  .arc circle {
    fill: none;
    stroke: var(--st-working);
    stroke-width: 3;
    stroke-linecap: round;
    stroke-dasharray: 70 30;
  }
  .st-waiting .ring { border-style: dashed; border-color: var(--st-waiting); }
  .st-done .ring { border-color: var(--st-done); }
  .st-unknown .ring { border-color: var(--st-unknown); }
  .st-blocked .ring {
    border-color: var(--st-blocked);
    background: color-mix(in srgb, var(--st-blocked) 22%, transparent);
  }
  /* The blocked pulse is a halo layer animated by transform + opacity only
     (compositor work); animating box-shadow repainted the item every frame. */
  .st-blocked .ring::after {
    content: "";
    position: absolute;
    inset: -3px;
    border-radius: 50%;
    border: 3px solid color-mix(in srgb, var(--st-blocked) 55%, transparent);
    opacity: 0;
    animation: dock-pulse 1.8s var(--ease) infinite;
    will-change: transform, opacity;
  }
  .done-dot {
    position: absolute;
    top: 1px;
    right: 1px;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--st-done);
    box-shadow: 0 0 0 2px var(--panel);
  }
  .offline .face, .offline .ring, .offline .arc { filter: grayscale(1) opacity(.55); }
  .slash {
    position: absolute;
    width: 2px;
    height: 50px;
    background: var(--st-offline);
    transform: rotate(45deg);
    pointer-events: none;
  }
  /* A project group's member count: a small pill cut out of the ring at the
     bottom right (the done dot owns the top right). */
  .count {
    position: absolute;
    right: -2px;
    bottom: -2px;
    min-width: 17px;
    height: 17px;
    box-sizing: border-box;
    padding: 0 4px;
    border-radius: 9px;
    display: grid;
    place-items: center;
    background: var(--key);
    color: var(--text);
    font: 700 10px/1 var(--font-ui);
    font-variant-numeric: tabular-nums;
    box-shadow: 0 0 0 2px var(--panel), inset 0 0 0 1px var(--line-strong);
    pointer-events: none;
  }
  .active .ring, .item:focus-visible .ring { box-shadow: 0 0 0 2px var(--accent-ring); }
  .item:focus-visible { outline: none; }
  @keyframes dock-spin { to { transform: rotate(360deg); } }
  @keyframes dock-pulse {
    0% { transform: scale(1); opacity: .9; }
    60%, 100% { transform: scale(1.22); opacity: 0; }
  }
  :global(.mini-dock.reduced) .arc,
  :global(.mini-dock.reduced) .ring::after { animation: none; }
  @media (prefers-reduced-motion: reduce) {
    .arc, .ring::after { animation: none; }
  }
</style>
