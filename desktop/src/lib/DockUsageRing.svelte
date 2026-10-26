<script lang="ts">
  // One usage provider in the dock strip's footer: a 32 px badge whose outer
  // ring is what is LEFT of the shortest window (full = fresh; the empty part
  // grows clockwise from 12 o'clock as it is used) and whose thinner inner
  // ring is the longest window, around the provider's 14 px mark. Static —
  // only a value change eases the arc once; nothing animates continuously.
  import UsageMark from "./UsageMark.svelte";
  import { ringArc, ringAriaLabel, type UsageRing } from "./dockUsage";
  import { locale } from "./i18n.svelte";

  let {
    ring,
    markSrc,
    active = false,
    onenter,
    onleave,
    onfocus,
    onblur,
    onclick,
  }: {
    ring: UsageRing;
    markSrc: string | null;
    active?: boolean;
    onenter: (top: number) => void;
    onleave: () => void;
    onfocus: (top: number) => void;
    onblur: () => void;
    onclick: (top: number) => void;
  } = $props();

  const outer = $derived(ringArc(ring.outer.left));
  const inner = $derived(ring.inner ? ringArc(ring.inner.left) : null);
  const top = (event: Event): number => (event.currentTarget as HTMLElement).offsetTop;
</script>

<button
  type="button"
  class="usage-ring"
  class:active
  data-provider={ring.provider}
  aria-label={ringAriaLabel(locale.lang, ring)}
  onmouseenter={(e) => onenter(top(e))}
  onmouseleave={onleave}
  onfocus={(e) => onfocus(top(e))}
  onblur={onblur}
  onclick={(e) => onclick(top(e))}
>
  <!-- Radii and widths put every stroke edge on a whole pixel of the 32 px
       box (outer 13–16, inner 9–11) with 2 px gaps, so the rings stay crisp. -->
  <svg viewBox="0 0 32 32" width="32" height="32" aria-hidden="true">
    <g transform="rotate(-90 16 16)">
      <circle class="track outer" cx="16" cy="16" r="14.5" style:--c={ring.outer.color} />
      {#if outer}
        <circle
          class="arc outer tone-{ring.outer.tone}"
          cx="16"
          cy="16"
          r="14.5"
          pathLength="100"
          style:--c={ring.outer.color}
          style:stroke-dasharray={outer.dash}
          style:stroke-dashoffset={outer.offset}
        />
      {/if}
      {#if ring.inner}
        <circle class="track inner" cx="16" cy="16" r="10" style:--c={ring.inner.color} />
        {#if inner}
          <circle
            class="arc inner tone-{ring.inner.tone}"
            cx="16"
            cy="16"
            r="10"
            pathLength="100"
            style:--c={ring.inner.color}
            style:stroke-dasharray={inner.dash}
            style:stroke-dashoffset={inner.offset}
          />
        {/if}
      {/if}
    </g>
  </svg>
  <span class="centre"><UsageMark provider={ring.provider} src={markSrc} color={ring.color} size={14} /></span>
</button>

<style>
  .usage-ring {
    position: relative;
    width: 40px;
    height: 40px;
    display: grid;
    place-items: center;
    padding: 0;
    border: 0;
    border-radius: 50%;
    background: transparent;
    cursor: pointer;
    flex: none;
  }
  svg {
    grid-area: 1 / 1;
    display: block;
    overflow: visible;
  }
  .centre {
    grid-area: 1 / 1;
    display: grid;
    place-items: center;
  }
  circle { fill: none; }
  .outer { stroke-width: 3; }
  .inner { stroke-width: 2; }
  .track { stroke: color-mix(in srgb, var(--c) 16%, transparent); }
  .arc {
    stroke: var(--c);
    stroke-linecap: round;
    /* One ease when a poll moves the value; never a running animation. */
    transition: stroke-dasharray 600ms var(--ease), stroke-dashoffset 600ms var(--ease), stroke 600ms var(--ease);
  }
  .usage-ring:hover { background: var(--key); }
  .active, .usage-ring:focus-visible { box-shadow: 0 0 0 2px var(--accent-ring); outline: none; }
  :global(.mini-dock.reduced) .arc { transition: none; }
  @media (prefers-reduced-motion: reduce) {
    .arc { transition: none; }
  }
</style>
