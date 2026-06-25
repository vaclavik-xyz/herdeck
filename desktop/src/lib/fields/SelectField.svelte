<script lang="ts">
  let { label, value, options, onchange }:
    { label: string; value: string; options: string[]; onchange: (v: string) => void } = $props();

  // Surface an unknown stored value rather than silently snapping to options[0].
  const choices = $derived(options.includes(value) ? options : [value, ...options]);
</script>

<label class="field">
  <span>{label}</span>
  <select value={value} onchange={(e) => onchange((e.target as HTMLSelectElement).value)}>
    {#each choices as o}<option value={o}>{o}</option>{/each}
  </select>
</label>

<style>
  .field { display: grid; grid-template-columns: 120px 1fr; align-items: center; gap: 8px; margin: 4px 0; }
  .field span { color: #aaa; }
  select { background: #141417; border: 1px solid #2a2a30; color: inherit; padding: 4px 6px; border-radius: 4px; }
</style>
