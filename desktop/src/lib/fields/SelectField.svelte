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
  .field {
    display: grid;
    grid-template-columns: var(--field-label-w) minmax(0, 1fr);
    gap: var(--s1) var(--s6);
    padding: var(--s3) 0;
    border-bottom: 1px solid var(--line);
  }
  .field.unlabelled {
    grid-template-columns: minmax(0, 1fr);
    padding: 0;
    border-bottom: 0;
  }
  select {
    grid-column: 2;
    grid-row: 1 / span 2;
    align-self: center;
    width: 100%;
    max-width: var(--control-md);
    min-height: 32px;
    padding: 0 var(--s6) 0 var(--s3);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--field);
    color: var(--text);
  }
  select:hover { border-color: var(--accent-ring); }
  .field.unlabelled select { grid-column: 1; grid-row: auto; }
  @media (max-width: 760px) {
    .field { grid-template-columns: minmax(0, 1fr); }
    select { grid-column: 1; grid-row: auto; max-width: none; }
  }
</style>
