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
    onenter,
    onleave,
    onfocus,
    onblur,
    onclick,
  }: {
    agent: DockAgent;
    iconSrc: string | null;
    active?: boolean;
    onenter: (top: number) => void;
    onleave: () => void;
    onfocus: (top: number) => void;
    onblur: () => void;
    onclick: (top: number) => void;
  } = $props();

  const label = $derived(`${agent.title} · ${statusLabel(locale.lang, agent.status, agent.offline)}`);
  const top = (event: Event): number => (event.currentTarget as HTMLElement).offsetTop;
</script>

<button
  type="button"
  class="item st-{agent.status}"
  class:offline={agent.offline}
  class:active
  title={label}
  aria-label={label}
  onmouseenter={(e) => onenter(top(e))}
  onmouseleave={onleave}
  onfocus={(e) => onfocus(top(e))}
  onblur={onblur}
  onclick={(e) => onclick(top(e))}
>
  <span class="ring" aria-hidden="true"></span>
  <span class="face"><DockMark {agent} src={iconSrc} size={30} /></span>
  {#if agent.status === "done"}<span class="done-dot" aria-hidden="true"></span>{/if}
  {#if agent.offline}<span class="slash" aria-hidden="true"></span>{/if}
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
  .st-working .ring {
    border-color: var(--st-working);
    border-right-color: transparent;
    animation: dock-spin 1.6s linear infinite;
  }
  .st-waiting .ring { border-style: dashed; border-color: var(--st-waiting); }
  .st-done .ring { border-color: var(--st-done); }
  .st-unknown .ring { border-color: var(--st-unknown); }
  .st-blocked .ring {
    border-color: var(--st-blocked);
    background: color-mix(in srgb, var(--st-blocked) 22%, transparent);
    animation: dock-pulse 1.8s var(--ease) infinite;
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
  .offline .face, .offline .ring { filter: grayscale(1) opacity(.55); }
  .slash {
    position: absolute;
    width: 2px;
    height: 50px;
    background: var(--st-offline);
    transform: rotate(45deg);
    pointer-events: none;
  }
  .active .ring, .item:focus-visible .ring { box-shadow: 0 0 0 2px var(--accent-ring); }
  .item:focus-visible { outline: none; }
  @keyframes dock-spin { to { transform: rotate(360deg); } }
  @keyframes dock-pulse {
    0%, 100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--st-blocked) 45%, transparent); }
    50% { box-shadow: 0 0 0 5px color-mix(in srgb, var(--st-blocked) 0%, transparent); }
  }
  :global(.mini-dock.reduced) .ring { animation: none; }
  @media (prefers-reduced-motion: reduce) {
    .ring { animation: none; }
  }
</style>
