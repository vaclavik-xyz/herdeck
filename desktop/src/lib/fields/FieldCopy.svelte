<script lang="ts">
  import { fieldPresentation } from "../fieldPresentation";
  import { locale } from "../i18n.svelte";

  let { label, help = "" }: { label: string; help?: string } = $props();
  const presentation = $derived(fieldPresentation(label, locale.lang));
</script>

<span
  class="label fieldlabel"
  class:hashelp={!!help}
  data-config-key={presentation.configKey ?? undefined}
  title={help || undefined}
>
  <span>{presentation.label}</span>
  {#if presentation.configKey}<code>{presentation.configKey}</code>{/if}
</span>
{#if help}<small class="fieldhelp">{help}</small>{/if}

<style>
  .fieldlabel { display: flex; grid-column: 1; grid-row: 1; align-self: end; min-width: 0; flex-wrap: wrap; align-items: baseline; gap: 5px; color: #d8dde4; font-size: 11px; font-weight: 620; overflow-wrap: anywhere; }
  .fieldlabel code { color: var(--muted-low, #6f7885); font: 8px "SF Mono", ui-monospace, monospace; font-weight: 500; }
  .fieldlabel.hashelp { cursor: help; }
  .fieldhelp { grid-column: 1; grid-row: 2; align-self: start; max-width: 36rem; color: var(--muted, #939ca9); font-size: 9px; line-height: 1.45; }
</style>
