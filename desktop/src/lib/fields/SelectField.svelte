<script lang="ts">
  import FieldCopy from "./FieldCopy.svelte";
  let { label, value, options, onchange, help = "", owner = null }:
    { label: string; value: string; options: string[]; onchange: (v: string) => void; help?: string; owner?: string | null } = $props();

  // Surface an unknown stored value rather than silently snapping to options[0].
  const choices = $derived(options.includes(value) ? options : [value, ...options]);
</script>

<label class="field" class:unlabelled={!label}>
  {#if label}<FieldCopy {label} {help} {owner} />{/if}
  <select value={value} onchange={(e) => onchange((e.target as HTMLSelectElement).value)}>
    {#each choices as o}<option value={o}>{o}</option>{/each}
  </select>
</label>

<style>
  .field { display: grid; grid-template-columns: var(--field-label-w, 120px) minmax(0, 1fr); align-items: center; gap: 12px; margin: 4px 0; }
  .field.unlabelled { grid-template-columns: minmax(0, 1fr); }
  select { grid-column: 2; grid-row: 1 / span 2; width: 100%; background: #141417; border: 1px solid #2a2a30; color: inherit; padding: 4px 6px; border-radius: 4px; }
  .field.unlabelled select { grid-column: 1; grid-row: auto; }
</style>
