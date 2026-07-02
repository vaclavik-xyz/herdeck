<script lang="ts">
  // `help` is the Czech tooltip explaining what the setting does. Every field
  // with a visible label must pass one — enforced by sections.help.test.ts.
  let { label, value, oninput, help = "" }:
    { label: string; value: string; oninput: (v: string) => void; help?: string } = $props();
</script>

<label class="field">
  <span class="fieldlabel" class:hashelp={!!help} title={help || undefined}>{label}</span>
  <input value={value} oninput={(e) => oninput((e.target as HTMLInputElement).value)} />
</label>

<style>
  .field { display: grid; grid-template-columns: var(--field-label-w, 120px) 1fr; align-items: center; gap: 8px; margin: 4px 0; }
  .field span { color: #aaa; }
  .fieldlabel.hashelp { text-decoration: underline dotted #5a5a62; text-underline-offset: 3px; cursor: help; }
  input { background: #141417; border: 1px solid #2a2a30; color: inherit; padding: 4px 6px; border-radius: 4px; }
</style>
