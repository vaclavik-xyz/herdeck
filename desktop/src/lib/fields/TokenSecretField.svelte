<script lang="ts">
  import { t } from "../i18n.svelte";
  import type { SecretFlag } from "../configClient";
  import FieldCopy from "./FieldCopy.svelte";
  let {
    label,
    value,
    flag,
    oninput,
    onset,
    onclear,
    help = "",
    owner = null,
  }: {
    label: string;
    value: string;
    flag: SecretFlag;
    oninput: (v: string) => void;
    onset: (secretValue: string) => void;
    onclear: () => void;
    help?: string;
    owner?: string | null;
  } = $props();

  let entering = $state(false);
  let secretValue = $state("");

  // Reset any in-progress secret entry when the token identity changes — e.g. a row
  // removal/shift or an external reload reuses this field for a DIFFERENT server, so
  // the half-typed value must not be submittable to the wrong token. Tracks `value`.
  $effect(() => {
    value; // dependency: the token_env this field is bound to
    entering = false;
    secretValue = "";
  });

  function submit(): void {
    if (secretValue) onset(secretValue);
    secretValue = "";
    entering = false;
  }
</script>

<label class="field">
  <FieldCopy {label} {help} {owner} />
  <input value={value} oninput={(e) => oninput((e.target as HTMLInputElement).value)} />
  {#if value}
    {#if flag.set}
      <span class="ok" title={flag.source ?? ""}>🔑✓</span>
      {#if flag.source === "keychain"}
        <button type="button" onclick={onclear}>{t("widget.clear")}</button>
      {/if}
    {:else}
      <span class="missing">🔑✗</span>
      <button type="button" onclick={() => (entering = true)}>{t("widget.set")}</button>
    {/if}
  {/if}
</label>

{#if entering}
  <div class="setrow">
    <input type="password" placeholder={t("widget.token_value")} bind:value={secretValue} />
    <button type="button" onclick={submit}>{t("widget.save_keychain")}</button>
    <button type="button" onclick={() => (entering = false)}>{t("widget.cancel")}</button>
  </div>
{/if}

<style>
  .field { display: grid; grid-template-columns: var(--field-label-w, 120px) minmax(0, 1fr) auto auto; align-items: center; gap: 8px; margin: 4px 0; }
  .field > input { grid-column: 2; grid-row: 1 / span 2; background: var(--field); border: 1px solid var(--line); color: inherit; padding: 4px 6px; border-radius: var(--r-control); }
  .ok { color: var(--form-ok); } .missing { color: var(--form-missing); }
  .setrow { display: flex; gap: 8px; margin: 4px 0 8px calc(var(--field-label-w, 120px) + 8px); }
  button { background: var(--panel-raised); border: 1px solid var(--line); color: inherit; border-radius: var(--r-control); cursor: pointer; }
  /* Without this the 240px label track stayed fixed on a phone and this field
     alone pushed the whole Connections page into a horizontal scroll. */
  @media (max-width: 760px) {
    .field { grid-template-columns: minmax(0, 1fr) auto; }
    .field > input { grid-column: 1 / -1; grid-row: auto; }
    .setrow { flex-wrap: wrap; margin-left: 0; }
    .setrow input { flex: 1 1 100%; min-width: 0; }
    button { min-height: 32px; padding: 0 var(--s3); }
  }
</style>
