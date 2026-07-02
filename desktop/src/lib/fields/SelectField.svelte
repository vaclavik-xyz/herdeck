<script lang="ts">
  let { label, value, options, onchange, help = "" }:
    { label: string; value: string; options: string[]; onchange: (v: string) => void; help?: string } = $props();

  // Surface an unknown stored value rather than silently snapping to options[0].
  const choices = $derived(options.includes(value) ? options : [value, ...options]);
</script>

<label class="field">
  <span class="fieldlabel" class:hashelp={!!help} title={help || undefined}>{label}</span>
  <select value={value} onchange={(e) => onchange((e.target as HTMLSelectElement).value)}>
    {#each choices as o}<option value={o}>{o}</option>{/each}
  </select>
</label>

<style>
  .field { display: grid; grid-template-columns: var(--field-label-w, 120px) 1fr; align-items: center; gap: 8px; margin: 4px 0; }
  .field span { color: #aaa; }
  .fieldlabel.hashelp { text-decoration: underline dotted #5a5a62; text-underline-offset: 3px; cursor: help; }
  select { background: #141417; border: 1px solid #2a2a30; color: inherit; padding: 4px 6px; border-radius: 4px; }
</style>
