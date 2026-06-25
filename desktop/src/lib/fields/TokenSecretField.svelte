<script lang="ts">
  import type { SecretFlag } from "../configClient";
  let {
    label,
    value,
    flag,
    oninput,
    onset,
    onclear,
  }: {
    label: string;
    value: string;
    flag: SecretFlag;
    oninput: (v: string) => void;
    onset: (secretValue: string) => void;
    onclear: () => void;
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
  <span>{label}</span>
  <input value={value} oninput={(e) => oninput((e.target as HTMLInputElement).value)} />
  {#if value}
    {#if flag.set}
      <span class="ok" title={flag.source ?? ""}>🔑✓</span>
      {#if flag.source === "keychain"}
        <button type="button" onclick={onclear}>clear</button>
      {/if}
    {:else}
      <span class="missing">🔑✗</span>
      <button type="button" onclick={() => (entering = true)}>nastav</button>
    {/if}
  {/if}
</label>

{#if entering}
  <div class="setrow">
    <input type="password" placeholder="hodnota tokenu" bind:value={secretValue} />
    <button type="button" onclick={submit}>Uložit do keychain</button>
    <button type="button" onclick={() => (entering = false)}>Zrušit</button>
  </div>
{/if}

<style>
  .field { display: grid; grid-template-columns: 80px 1fr auto auto; align-items: center; gap: 8px; margin: 4px 0; }
  .field span:first-child { color: #aaa; }
  input { background: #141417; border: 1px solid #2a2a30; color: inherit; padding: 4px 6px; border-radius: 4px; }
  .ok { color: #4fa84f; } .missing { color: #e0a030; }
  .setrow { display: flex; gap: 8px; margin: 4px 0 8px 88px; }
  button { background: #1b1b1f; border: 1px solid #2a2a30; color: inherit; border-radius: 4px; cursor: pointer; }
</style>
