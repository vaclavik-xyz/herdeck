<script lang="ts">
  // Status colours resolve STRICTLY through the backend's named palette
  // (COLORS.get(name, dim)), so this picker offers the palette itself instead
  // of a text/dropdown value a typo could slip through. A stored value outside
  // the palette is shown as an explicit "unknown" chip, never silently mapped.
  import { t } from "../i18n.svelte";
  import { PALETTE, PALETTE_NAMES } from "../statusColors";
  import FieldCopy from "./FieldCopy.svelte";

  let { label, value, onchange, allowEmpty = true, help = "" }:
    { label: string; value: string; onchange: (v: string) => void; allowEmpty?: boolean; help?: string } = $props();

  const known = $derived(value === "" || PALETTE_NAMES.includes(value));
</script>

<div class="field" class:unlabelled={!label}>
  {#if label}<FieldCopy {label} {help} />{/if}
  <div class="swatches" role="radiogroup" aria-label={label}>
    {#if allowEmpty}
      <!-- Not a palette entry, so not part of the radiogroup's radio count:
           "(default)" clears the override rather than selecting a colour. -->
      <button
        type="button"
        class="swatch empty"
        data-color=""
        aria-pressed={value === ""}
        title={t("widget.default_empty")}
        onclick={() => onchange("")}
      >{t("widget.default_empty")}</button>
    {/if}
    {#each PALETTE_NAMES as name (name)}
      <button
        type="button"
        role="radio"
        class="swatch"
        data-color={name}
        aria-checked={value === name}
        aria-label={name}
        title={name}
        style={`--swatch:${PALETTE[name]}`}
        onclick={() => onchange(name)}
      ></button>
    {/each}
    {#if !known}<span class="unknown">{value}</span>{/if}
  </div>
</div>

<style>
  .field {
    display: grid;
    grid-template-columns: var(--field-label-w) minmax(0, 1fr);
    align-items: start;
    gap: var(--s1) var(--s6);
    padding: var(--s3) 0;
    border-bottom: 1px solid var(--line);
  }
  .field.unlabelled { grid-template-columns: minmax(0, 1fr); padding: 0; border-bottom: 0; }
  .swatches {
    display: flex;
    grid-column: 2;
    grid-row: 1 / span 2;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--s2);
  }
  .field.unlabelled .swatches { grid-column: 1; grid-row: auto; }
  .swatch {
    width: 26px;
    height: 26px;
    padding: 0;
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--swatch, var(--field));
    cursor: pointer;
    transition: box-shadow var(--dur) var(--ease), transform var(--dur) var(--ease);
  }
  .swatch:hover { transform: translateY(-1px); }
  .swatch[aria-checked="true"],
  .swatch[aria-pressed="true"] {
    box-shadow: 0 0 0 2px var(--canvas), 0 0 0 4px var(--accent-strong);
  }
  .swatch.empty {
    width: auto;
    padding: 0 var(--s2);
    color: var(--text-dim);
    font: var(--t-help);
  }
  .unknown {
    padding: 2px var(--s2);
    border: 1px solid var(--st-offline);
    border-radius: var(--r-control);
    color: var(--st-offline);
    font: var(--t-mono);
  }
</style>
