<script lang="ts">
  let {
    label,
    value,
    onchange,
    int = false,
    step = 1,
  }: {
    label: string;
    value: number | null;
    onchange: (v: number | null) => void;
    int?: boolean;
    step?: number;
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

<label class="field">
  <span>{label}</span>
  <input
    type="number"
    {step}
    value={value ?? ""}
    onchange={(e) => emit((e.target as HTMLInputElement).value)}
  />
</label>

<style>
  .field { display: grid; grid-template-columns: 120px 1fr; align-items: center; gap: 8px; margin: 4px 0; }
  .field span { color: #aaa; }
  input { background: #141417; border: 1px solid #2a2a30; color: inherit; padding: 4px 6px; border-radius: 4px; }
</style>
