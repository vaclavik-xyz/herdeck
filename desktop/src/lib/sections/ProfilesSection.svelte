<script lang="ts">
  import SelectField from "../fields/SelectField.svelte";
  import OverrideField from "../fields/OverrideField.svelte";
  import {
    profileNames, createProfile, deleteProfile,
    profileExtends, setProfileExtends, profileServers,
    profileServersState, setProfileServersExplicit, clearProfileServers, effectiveProfileServers,
    serversOf, type ConfigPayload,
  } from "../configClient";

  let { payload = $bindable(), onChange, onError }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  let newName = $state("");

  // Czech tooltips for every field — required for each labelled field
  // (enforced by sections.help.test.ts).
  const HELP: Record<string, string> = {
    extends: "Ze kterého profilu tento profil dědí nastavení; „default“ znamená přímo základní konfiguraci.",
    servers: "Které servery profil používá; při dědění přebírá výběr rodiče či báze, prázdný výběr = profil bez serverů.",
  };

  const names = $derived(profileNames(payload));
  const serverIds = $derived(serversOf(payload).map((s) => s.id).filter((id) => id !== ""));

  // Checkbox options for a profile = current base servers PLUS any stale refs this profile
  // still holds (so an unknown/removed server id can be unchecked away — otherwise it is
  // invisible and the backend rejects Apply as "unknown server" with no way to fix it).
  function serverOptions(name: string): { id: string; known: boolean }[] {
    const sel = profileServers(payload, name);
    const stale = sel.filter((id) => !serverIds.includes(id));
    return [
      ...serverIds.map((id) => ({ id, known: true })),
      ...stale.map((id) => ({ id, known: false })),
    ];
  }

  // A profile may extend "default" (base) or any OTHER profile — never itself.
  function extendsOptions(self: string): string[] {
    return ["default", ...names.filter((n) => n !== self)];
  }

  function create(): void {
    const res = createProfile(payload, newName);
    if (!res.ok) { onError(res.error); return; }
    payload = res.payload;
    newName = "";
    onChange();
  }
  function remove(name: string): void {
    // An env lock (HERDECK_PROFILE) pins the active profile and can't be cleared from
    // the editor — deleting it would leave the lock pointing at a missing profile. Block it.
    if (payload.envLocked && payload.activeProfile === name) {
      onError("nelze smazat profil zamčený přes HERDECK_PROFILE");
      return;
    }
    payload = deleteProfile(payload, name);
    onChange();
  }
  function setExtends(name: string, ext: string): void {
    payload = setProfileExtends(payload, name, ext);
    onChange();
  }
  function srvState(name: string): "inherit" | "override" { return profileServersState(payload, name) === "explicit" ? "override" : "inherit"; }
  function setSrvState(name: string, s: "inherit" | "override"): void {
    // Toggling to explicit seeds the EFFECTIVE inherited selection (mirrors backend resolution:
    // nearest parent profile servers → merged deck.overview_order → all base servers) so the
    // profile doesn't silently gain/lose servers on the toggle; the user then unchecks to
    // restrict, or unchecks all for an intentional serverless ([]) profile.
    payload = s === "inherit"
      ? clearProfileServers(payload, name)
      : setProfileServersExplicit(payload, name, effectiveProfileServers(payload, name));
    onChange();
  }
  function toggleServer(name: string, id: string, on: boolean): void {
    const cur = profileServers(payload, name);
    const next = on ? [...cur, id] : cur.filter((s) => s !== id);
    payload = setProfileServersExplicit(payload, name, next); // explicit: keeps [] (serverless)
    onChange();
  }
</script>

<h2>Profily</h2>
<p class="hint">Pojmenované profily překrývají bázi. Aktivní profil se vybírá nahoře; per-sekce overrides jsou řez 4b-ii.</p>

<div class="create">
  <input placeholder="jméno nového profilu" bind:value={newName} />
  <button type="button" onclick={create}>+ vytvořit profil</button>
</div>

{#each names as name (name)}
  <fieldset>
    <legend>{name} <button type="button" title="Smazat profil" onclick={() => remove(name)}>×</button></legend>
    <SelectField
      label="extends"
      help={HELP.extends}
      value={profileExtends(payload, name)}
      options={extendsOptions(name)}
      onchange={(v) => setExtends(name, v)}
    />
    <OverrideField label="servers" help={HELP.servers} state={srvState(name)} inheritedDisplay="zdědí base servery" onstate={(s) => setSrvState(name, s)}>
      <div class="servers">
        {#if serverOptions(name).length === 0}
          <span class="hint">žádné servery v bázi — přidej je v sekci Servery</span>
        {:else}
          {#each serverOptions(name) as opt (opt.id)}
            <label class="chk">
              <input
                type="checkbox"
                checked={profileServers(payload, name).includes(opt.id)}
                onchange={(e) => toggleServer(name, opt.id, (e.target as HTMLInputElement).checked)}
              />
              {opt.id}{#if !opt.known} <span class="unknown">(neznámý)</span>{/if}
            </label>
          {/each}
          {#if profileServers(payload, name).length === 0}
            <span class="hint">serverless: profil poběží bez serverů (explicitní prázdný výběr)</span>
          {/if}
        {/if}
      </div>
    </OverrideField>
  </fieldset>
{/each}
{#if names.length === 0}
  <p class="hint">Zatím žádný profil. Vytvoř první výše.</p>
{/if}

<style>
  h2 { margin: 0 0 8px; }
  .hint { color: #888; margin: 0 0 8px; }
  .create { display: flex; gap: 6px; margin: 8px 0; }
  .create input { flex: 1; background: #141417; border: 1px solid #2a2a30; color: inherit; padding: 4px 6px; border-radius: 4px; }
  fieldset { border: 1px solid #2a2a30; border-radius: 6px; margin: 8px 0; padding: 8px 12px; }
  legend { color: #ccc; } legend button { color: #e05050; background: none; border: 0; cursor: pointer; }
  .servers { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; margin: 2px 0; }
  .chk { display: inline-flex; align-items: center; gap: 4px; margin-right: 12px; color: #ccc; }
  .unknown { color: #e05050; font-size: 11px; }
  button { background: #1b1b1f; border: 1px solid #2a2a30; color: inherit; border-radius: 4px; padding: 4px 8px; cursor: pointer; }
</style>
