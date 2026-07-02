<script lang="ts">
  let { label, value, onchange, help = "" }:
    { label: string; value: boolean; onchange: (v: boolean) => void; help?: string } = $props();
</script>

<label class="field">
  <input
    type="checkbox"
    checked={value}
    onchange={(e) => onchange((e.target as HTMLInputElement).checked)}
  />
  <span class="fieldlabel" class:hashelp={!!help} title={help || undefined}>{label}</span>
</label>

<style>
  .field { display: flex; align-items: center; gap: 8px; margin: 6px 0; cursor: pointer; }
  .field span { color: #ccc; }
  .fieldlabel.hashelp { text-decoration: underline dotted #5a5a62; text-underline-offset: 3px; cursor: help; }
</style>
