<script lang="ts">
  // A notification sound name: a select over the sounds the OS actually has
  // (from the `notification_sounds` command) with a free-text fallback when
  // that list is empty or unavailable (non-macOS, plain browser, older shell),
  // plus a play button that fires a test notification with this sound.
  import FieldCopy from "./FieldCopy.svelte";

  let {
    label,
    value,
    options = [],
    placeholder = "",
    defaultLabel,
    testLabel,
    onchange,
    ontest = undefined,
    help = "",
    owner = null,
  }: {
    label: string;
    value: string;
    options?: string[];
    placeholder?: string;
    /** Text of the empty choice, e.g. "(default: Glass)". */
    defaultLabel: string;
    /** Title/aria-label of the play button (translated by the caller). */
    testLabel: string;
    onchange: (v: string) => void;
    ontest?: (() => void) | undefined;
    help?: string;
    owner?: string | null;
  } = $props();

  // Surface a hand-written name the OS list does not know rather than
  // silently snapping it to the default.
  const choices = $derived(value === "" || options.includes(value) ? options : [value, ...options]);
</script>

<label class="field" class:unlabelled={!label}>
  {#if label}<FieldCopy {label} {help} {owner} />{/if}
  <span class="control">
    {#if options.length > 0}
      <select value={value} onchange={(e) => onchange((e.target as HTMLSelectElement).value)}>
        <option value="">{defaultLabel}</option>
        {#each choices as o}<option value={o}>{o}</option>{/each}
      </select>
    {:else}
      <input value={value} {placeholder} oninput={(e) => onchange((e.target as HTMLInputElement).value)} />
    {/if}
    {#if ontest}
      <button type="button" class="test" title={testLabel} aria-label={testLabel} onclick={(e) => { e.preventDefault(); ontest?.(); }}>▶</button>
    {/if}
  </span>
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
  .control {
    display: flex;
    grid-column: 2;
    grid-row: 1 / span 2;
    align-self: center;
    gap: var(--s2);
    min-width: 0;
    max-width: var(--control-md);
  }
  .field.unlabelled .control { grid-column: 1; grid-row: auto; }
  select, input {
    flex: 1;
    min-width: 0;
    min-height: 32px;
    padding: 0 var(--s3);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--field);
    color: var(--text);
  }
  select:hover, input:hover { border-color: var(--accent-ring); }
  .test {
    flex: none;
    width: 32px;
    min-height: 32px;
    padding: 0;
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--panel-raised);
    color: var(--text);
    cursor: pointer;
  }
  .test:hover { background: var(--key); }
  @media (max-width: 760px) {
    .field { grid-template-columns: minmax(0, 1fr); }
    .control { grid-column: 1; grid-row: auto; max-width: none; }
  }
  @container settings-form (max-width: 600px) {
    .field { grid-template-columns: minmax(0, 1fr); }
    .control { grid-column: 1; grid-row: auto; max-width: none; }
  }
</style>
