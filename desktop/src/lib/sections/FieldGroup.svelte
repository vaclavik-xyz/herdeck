<script lang="ts">
  // The one grouping primitive for editor sections. Replaces ad-hoc <fieldset>
  // blocks whose legends leaked raw TOML keys ("colors") and mixed casing.
  import type { Snippet } from "svelte";

  let { title, description = "", children }:
    { title: string; description?: string; children?: Snippet } = $props();
</script>

<section class="group">
  <h3>{title}</h3>
  {#if description}<p>{description}</p>{/if}
  <div class="group-body">{#if children}{@render children()}{/if}</div>
</section>

<style>
  .group { margin: 0 0 var(--s8); }
  h3 { margin: 0; font: var(--t-h2); letter-spacing: -.01em; }
  p { margin: var(--s1) 0 0; max-width: 60ch; color: var(--text-dim); font: var(--t-help); }
  .group-body { margin-top: var(--s3); border-top: 1px solid var(--line); }
  .group-body > :global(:last-child) { border-bottom: 0; }
</style>
