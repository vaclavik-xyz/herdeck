<script lang="ts">
  import FieldCopy from "./FieldCopy.svelte";
  // `help` is the Czech tooltip explaining what the setting does. Every field
  // with a visible label must pass one — enforced by sections.help.test.ts.
  let { label, value, oninput, help = "", owner = null }:
    { label: string; value: string; oninput: (v: string) => void; help?: string; owner?: string | null } = $props();
</script>

<label class="field" class:unlabelled={!label}>
  {#if label}<FieldCopy {label} {help} {owner} />{/if}
  <input value={value} oninput={(e) => oninput((e.target as HTMLInputElement).value)} />
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
  input {
    grid-column: 2;
    grid-row: 1 / span 2;
    align-self: center;
    width: 100%;
    max-width: var(--control-lg);
    min-height: 32px;
    padding: 0 var(--s3);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--field);
    color: var(--text);
  }
  input:hover { border-color: var(--accent-ring); }
  .field.unlabelled input { grid-column: 1; grid-row: auto; }
  @media (max-width: 760px) {
    .field { grid-template-columns: minmax(0, 1fr); }
    input { grid-column: 1; grid-row: auto; max-width: none; }
  }
</style>
