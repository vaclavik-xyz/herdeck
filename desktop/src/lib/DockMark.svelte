<script lang="ts">
  // A project's mark in the mini dock: its favicon (served by the runtime as
  // a PNG on the herdeck image scheme) or, when there is none or it fails to
  // load, the deck's monogram: first letter on a palette colour.
  import type { DockAgent } from "./dockClient";
  import { monogramColor, monogramInk } from "./dockModel";

  let { agent, src, size = 40 }: { agent: DockAgent; src: string | null; size?: number } = $props();

  let failed = $state<string | null>(null); // the src that failed to load
  const color = $derived(monogramColor(agent.repo || agent.title));
  const showImage = $derived(src !== null && failed !== src);
</script>

{#if showImage}
  <img class="mark" src={src} alt="" width={size} height={size} onerror={() => (failed = src)} />
{:else}
  <span
    class="mark mono ink-{monogramInk(color)}"
    style:width="{size}px"
    style:height="{size}px"
    style:font-size="{Math.round(size * 0.5)}px"
    style:background={color}
    aria-hidden="true">{agent.monogram}</span>
{/if}

<style>
  .mark {
    display: block;
    border-radius: 22%;
    object-fit: contain;
    flex: none;
  }
  .mono {
    display: grid;
    place-items: center;
    font-weight: 700;
    font-family: var(--font-ui);
    line-height: 1;
  }
  .ink-dark { color: var(--canvas); }
  .ink-light { color: var(--text); }
</style>
