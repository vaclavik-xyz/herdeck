<script lang="ts">
  import SelectField from "../fields/SelectField.svelte";
  import OverrideField from "../fields/OverrideField.svelte";
  import ConfirmRemoveButton from "../fields/ConfirmRemoveButton.svelte";
  import { defineMessages, fieldHelp, locale } from "../i18n.svelte";
  import {
    profileNames, createProfile, deleteProfile,
    profileExtends, setProfileExtends, profileServers,
    profileServersState, setProfileServersExplicit, clearProfileServers, effectiveProfileServers,
    serversOf, type ConfigPayload,
  } from "../configClient";

  let { payload = $bindable(), onChange, onError }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  let newName = $state("");

  // Tooltips for every field come from the central catalog in the current
  // language — required for each labelled field (enforced by sections.help.test.ts).
  const HELP = $derived(fieldHelp("profiles"));

  const LM = defineMessages({
    en: {
      intro: "Named profiles extend the base config. Choose the active profile from the toolbar and override only the settings it needs.",
      new_profile_name: "new profile name",
      create_profile: "+ create profile",
      remove_profile: "Remove profile",
      locked_delete: "cannot delete a profile locked via HERDECK_PROFILE",
      inherits_base_servers: "inherits base servers",
      no_base_servers: "no servers in the base. Add them in the Servers section",
      unknown_server: "(unknown)",
      serverless: "serverless: the profile will run with no servers (an explicit empty selection)",
      no_profiles: "No profiles yet. Create the first one above.",
    },
    cs: {
      intro: "Pojmenované profily překrývají bázi. Aktivní profil se vybírá nahoře; per-sekce overrides jsou řez 4b-ii.",
      new_profile_name: "jméno nového profilu",
      create_profile: "+ vytvořit profil",
      remove_profile: "Smazat profil",
      locked_delete: "nelze smazat profil zamčený přes HERDECK_PROFILE",
      inherits_base_servers: "zdědí base servery",
      no_base_servers: "žádné servery v bázi. Přidej je v sekci Servery",
      unknown_server: "(neznámý)",
      serverless: "serverless: profil poběží bez serverů (explicitní prázdný výběr)",
      no_profiles: "Zatím žádný profil. Vytvoř první výše.",
    },
  });
  const lm = $derived(LM[locale.lang]);

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
      onError(lm.locked_delete);
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

<p class="hint">{lm.intro}</p>

<div class="create">
  <input placeholder={lm.new_profile_name} bind:value={newName} />
  <button type="button" disabled={!newName.trim()} onclick={create}>{lm.create_profile}</button>
</div>

{#each names as name (name)}
  <fieldset>
    <legend>{name} <ConfirmRemoveButton title={lm.remove_profile} onconfirm={() => remove(name)} /></legend>
    <SelectField
      label="extends"
      help={HELP.extends}
      owner={name}
      value={profileExtends(payload, name)}
      options={extendsOptions(name)}
      onchange={(v) => setExtends(name, v)}
    />
    <OverrideField label="servers" help={HELP.servers} owner={name} state={srvState(name)} inheritedDisplay={lm.inherits_base_servers} onstate={(s) => setSrvState(name, s)}>
      <div class="servers">
        {#if serverOptions(name).length === 0}
          <span class="hint">{lm.no_base_servers}</span>
        {:else}
          {#each serverOptions(name) as opt (opt.id)}
            <label class="chk">
              <input
                type="checkbox"
                checked={profileServers(payload, name).includes(opt.id)}
                onchange={(e) => toggleServer(name, opt.id, (e.target as HTMLInputElement).checked)}
              />
              {opt.id}{#if !opt.known} <span class="unknown">{lm.unknown_server}</span>{/if}
            </label>
          {/each}
          {#if profileServers(payload, name).length === 0}
            <span class="hint">{lm.serverless}</span>
          {/if}
        {/if}
      </div>
    </OverrideField>
  </fieldset>
{/each}
{#if names.length === 0}
  <p class="hint">{lm.no_profiles}</p>
{/if}

<style>
  .hint { margin: 0 0 var(--s3); color: var(--text-dim); font: var(--t-help); }
  .create { display: flex; gap: var(--s2); margin: var(--s3) 0; }
  .create input {
    flex: 1;
    min-height: 30px;
    padding: 0 var(--s3);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--field);
    color: var(--text);
  }
  fieldset {
    margin: var(--s2) 0;
    padding: var(--s4) var(--s5);
    border: 1px solid var(--line);
    border-radius: var(--r-panel);
    background: var(--panel);
  }
  legend { color: var(--text); font: var(--t-label); }
  .servers { display: flex; flex-wrap: wrap; align-items: center; gap: var(--s2); margin: var(--s1) 0; }
  .chk { display: inline-flex; align-items: center; gap: var(--s1); margin-right: var(--s4); color: var(--text); }
  .unknown { color: var(--st-offline); font: var(--t-help); }
  button {
    min-height: 30px;
    padding: 0 var(--s3);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--field);
    color: var(--text-dim);
    cursor: pointer;
    transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
  }
  button:hover { color: var(--text); background: var(--panel-raised); }
</style>
