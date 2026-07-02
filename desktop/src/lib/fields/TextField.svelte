<script lang="ts">
  let { label, value, oninput }: { label: string; value: string; oninput: (v: string) => void } =
    $props();
</script>

<label class="field">
  <span>{label}</span>
  <input value={value} oninput={(e) => oninput((e.target as HTMLInputElement).value)} />
</label>

<style>
  .field { display: grid; grid-template-columns: var(--field-label-w, 120px) 1fr; align-items: center; gap: 8px; margin: 4px 0; }
  .field span { color: #aaa; }
  input { background: #141417; border: 1px solid #2a2a30; color: inherit; padding: 4px 6px; border-radius: 4px; }
</style>
