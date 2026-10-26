<script lang="ts">
  // A usage provider's logo: the runtime's agent mark (/agent-mark/<type>,
  // light ink on transparent, 96 px so it stays crisp at any zoom). A mark
  // that fails to load falls back to the provider's initial in its colour.
  // (Not a CSS mask tinted in the provider colour: mask images load in CORS
  // mode and the herdeck image scheme sends no CORS headers.)
  let {
    provider,
    src,
    color,
    size = 14,
  }: { provider: string; src: string | null; color: string; size?: number } = $props();

  let failed = $state<string | null>(null); // the src that failed to load
  const showMark = $derived(src !== null && failed !== src);
</script>

{#if showMark}
  <img class="mark" {src} alt="" width={size} height={size} onerror={() => (failed = src)} />
{:else}
  <span
    class="letter"
    style:width="{size}px"
    style:height="{size}px"
    style:font-size="{Math.round(size * 0.78)}px"
    style:color={color}
    aria-hidden="true">{provider.charAt(0).toUpperCase()}</span>
{/if}

<style>
  .mark { display: block; flex: none; object-fit: contain; }
  .letter {
    display: grid;
    place-items: center;
    flex: none;
    font-family: var(--font-ui);
    font-weight: 700;
    line-height: 1;
  }
</style>
