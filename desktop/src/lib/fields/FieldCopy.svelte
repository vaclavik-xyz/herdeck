<script lang="ts">
  let { label, help = "" }: { label: string; help?: string } = $props();
</script>

<span class="label fieldlabel" class:hashelp={!!help} title={help || undefined}>{label}</span>
{#if help}<small class="fieldhelp">{help}</small>{/if}

<style>
  .fieldlabel { grid-column: 1; grid-row: 1; align-self: end; min-width: 0; color: #d7dee8; font-size: 11px; font-weight: 620; overflow-wrap: anywhere; }
  .fieldlabel.hashelp { cursor: help; }
  .fieldhelp { grid-column: 1; grid-row: 2; align-self: start; max-width: 36rem; color: #7f8c9d; font-size: 9px; line-height: 1.45; }
</style>
