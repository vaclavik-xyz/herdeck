<script lang="ts">
  import { getContext } from "svelte";
  import { readable } from "svelte/store";

  import { fieldPresentation } from "../fieldPresentation";
  import { locale } from "../i18n.svelte";
  import { fieldValidationKey } from "../validationIssues";
  import { FIELD_VALIDATION_CONTEXT, type FieldValidationMessages } from "../validationContext";

  let { label, help = "", owner = null }: { label: string; help?: string; owner?: string | null } = $props();
  const presentation = $derived(fieldPresentation(label, locale.lang));
  const validations = getContext<FieldValidationMessages>(FIELD_VALIDATION_CONTEXT) ?? readable<Record<string, string[]>>({});
  const messages = $derived(presentation.configKey ? ($validations[fieldValidationKey(presentation.configKey, owner)] ?? []) : []);
</script>

<span
  class="label fieldlabel"
  class:hashelp={!!help}
  data-config-key={presentation.configKey ?? undefined}
  data-config-owner={owner ?? undefined}
  title={help || undefined}
>
  <span>{presentation.label}</span>
  {#if presentation.status}<span class="status">{presentation.status}</span>{/if}
  {#if presentation.configKey}<code>{presentation.configKey}</code>{/if}
</span>
{#if help}<small class="fieldhelp">{help}</small>{/if}
{#if messages.length > 0}<small class="fielderror" role="alert">{messages[0]}</small>{/if}

<style>
  .fieldlabel {
    display: flex;
    grid-column: 1;
    grid-row: 1;
    align-self: start;
    min-width: 0;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--s1) var(--s2);
    color: var(--text);
    font: var(--t-label);
    overflow-wrap: anywhere;
  }
  .fieldlabel code {
    color: var(--text-faint);
    font: var(--t-mono);
  }
  .fieldlabel .status {
    padding: 2px 6px;
    border: 1px solid var(--line-strong);
    border-radius: 999px;
    color: var(--text-dim);
    font: var(--t-eyebrow);
  }
  .fieldlabel.hashelp { cursor: help; }
  .fieldhelp {
    grid-column: 1;
    grid-row: 2;
    align-self: start;
    max-width: 34ch;
    color: var(--text-dim);
    font: var(--t-help);
  }
  .fielderror {
    grid-column: 2;
    grid-row: 3;
    color: var(--st-offline-text);
    font: var(--t-help);
  }
</style>
