<script lang="ts">
  import { getContext } from "svelte";
  import { readable } from "svelte/store";

  import { fieldPresentation } from "../fieldPresentation";
  import { locale } from "../i18n.svelte";
  import { FIELD_VALIDATION_CONTEXT, type FieldValidationMessages } from "../validationContext";

  let { label, help = "" }: { label: string; help?: string } = $props();
  const presentation = $derived(fieldPresentation(label, locale.lang));
  const validations = getContext<FieldValidationMessages>(FIELD_VALIDATION_CONTEXT) ?? readable<Record<string, string[]>>({});
  const messages = $derived(presentation.configKey ? ($validations[presentation.configKey] ?? []) : []);
</script>

<span
  class="label fieldlabel"
  class:hashelp={!!help}
  data-config-key={presentation.configKey ?? undefined}
  title={help || undefined}
>
  <span>{presentation.label}</span>
  {#if presentation.status}<span class="status">{presentation.status}</span>{/if}
  {#if presentation.configKey}<code>{presentation.configKey}</code>{/if}
</span>
{#if help}<small class="fieldhelp">{help}</small>{/if}
{#if messages.length > 0}<small class="fielderror" role="alert">{messages[0]}</small>{/if}

<style>
  .fieldlabel { display: flex; grid-column: 1; grid-row: 1; align-self: end; min-width: 0; flex-wrap: wrap; align-items: baseline; gap: 5px; color: #d8dde4; font-size: 11px; font-weight: 620; overflow-wrap: anywhere; }
  .fieldlabel code { color: var(--muted-low, #6f7885); font: 8px "SF Mono", ui-monospace, monospace; font-weight: 500; }
  .fieldlabel .status { border: 1px solid #343b45; border-radius: 999px; color: #9ba5b2; font-size: 8px; font-weight: 570; line-height: 1; padding: 2px 5px; }
  .fieldlabel.hashelp { cursor: help; }
  .fieldhelp { grid-column: 1; grid-row: 2; align-self: start; max-width: 36rem; color: var(--muted, #939ca9); font-size: 9px; line-height: 1.45; }
  .fielderror { grid-column: 1 / -1; grid-row: 3; color: var(--red, #db727a); font-size: 9px; line-height: 1.4; }
</style>
