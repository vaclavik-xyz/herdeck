<script lang="ts">
  // A named-palette colour picker with a live swatch. Status colours resolve
  // strictly through the backend's named palette — a free-text typo ('ambre')
  // used to pass Apply and silently render as the empty-tile 'dim' grey.
  import { PALETTE, PALETTE_NAMES } from "../statusColors";

  let { label, value, onchange, allowEmpty = true, help = "" }:
    { label: string; value: string; onchange: (v: string) => void; allowEmpty?: boolean; help?: string } = $props();

  // Surface an unknown stored value rather than silently snapping elsewhere.
  const choices = $derived(
    value === "" || PALETTE_NAMES.includes(value) ? PALETTE_NAMES : [value, ...PALETTE_NAMES],
  );
</script>

<label class="field">
  <span class="fieldlabel" class:hashelp={!!help} title={help || undefined}>{label}</span>
  <span class="control">
    <!-- STRICT palette lookup: the backend resolves status colours only via
         the named palette, so a legacy hex value must show as visibly invalid
         (transparent swatch), not as a working colour. -->
    <span class="swatch" style={`background:${PALETTE[value] ?? "transparent"}`}></span>
    <select value={value} onchange={(e) => onchange((e.target as HTMLSelectElement).value)}>
      {#if allowEmpty}<option value="">(výchozí)</option>{/if}
      {#each choices as o}<option value={o}>{o}</option>{/each}
    </select>
  </span>
</label>

<style>
  .field { display: grid; grid-template-columns: var(--field-label-w, 120px) 1fr; align-items: center; gap: 8px; margin: 4px 0; }
  .field > span:first-child { color: #aaa; }
  .fieldlabel.hashelp { text-decoration: underline dotted #5a5a62; text-underline-offset: 3px; cursor: help; }
  .control { display: flex; align-items: center; gap: 8px; }
  .swatch { width: 16px; height: 16px; border-radius: 4px; border: 1px solid #2a2a30; flex: none; }
  select { flex: 1; background: #141417; border: 1px solid #2a2a30; color: inherit; padding: 4px 6px; border-radius: 4px; }
</style>
