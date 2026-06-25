<script lang="ts">
  let { label, value, onchange }:
    { label: string; value: boolean; onchange: (v: boolean) => void } = $props();
</script>

<label class="field">
  <input
    type="checkbox"
    checked={value}
    onchange={(e) => onchange((e.target as HTMLInputElement).checked)}
  />
  <span>{label}</span>
</label>

<style>
  .field { display: flex; align-items: center; gap: 8px; margin: 6px 0; cursor: pointer; }
  .field span { color: #ccc; }
</style>
