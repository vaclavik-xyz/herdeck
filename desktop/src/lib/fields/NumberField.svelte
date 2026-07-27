<script lang="ts">
  import FieldCopy from "./FieldCopy.svelte";
  let {
    label,
    value,
    onchange,
    int = false,
    step = 1,
    min,
    max,
    help = "",
  }: {
    label: string;
    value: number | null;
    onchange: (v: number | null) => void;
    int?: boolean;
    step?: number | "any";
    min?: number;
    max?: number;
    help?: string;
  } = $props();

  // Commit on the DOM `change` event (blur/Enter), not per keystroke: a controlled
  // number input re-parsed on every keystroke turns "0." into "0", so decimals like
  // 0.25 are unenterable. Between commits the input holds its own raw text (the
  // `value` prop does not change, so Svelte never overwrites the focused field).
  // Parse with Number() (NOT parseInt/parseFloat, which truncate "1.9"→1 and accept
  // "1.2.3"→1.2): reject anything non-finite, and for int reject non-integers, → null.
  function emit(raw: string): void {
    const t = raw.trim();
    if (t === "") return onchange(null);
    const n = Number(t);
    if (!Number.isFinite(n) || (int && !Number.isInteger(n))) return onchange(null);
    onchange(n);
  }
</script>

<label class="field" class:unlabelled={!label}>
  {#if label}<FieldCopy {label} {help} />{/if}
  <input
    type="number"
    {step}
    {min}
    {max}
    value={value ?? ""}
    onchange={(e) => emit((e.target as HTMLInputElement).value)}
  />
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
    max-width: var(--control-sm);
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
