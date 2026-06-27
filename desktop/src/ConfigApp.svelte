<script lang="ts">
  import { onMount } from "svelte";
  import { invoke } from "@tauri-apps/api/core";
  import { listen } from "@tauri-apps/api/event";
  import DeckView from "./lib/DeckView.svelte";
  import ServersSection from "./lib/sections/ServersSection.svelte";
  import DeckSection from "./lib/sections/DeckSection.svelte";
  import ViewSection from "./lib/sections/ViewSection.svelte";
  import ThemeSection from "./lib/sections/ThemeSection.svelte";
  import MacrosSection from "./lib/sections/MacrosSection.svelte";
  import StartProfilesSection from "./lib/sections/StartProfilesSection.svelte";
  import NotificationsSection from "./lib/sections/NotificationsSection.svelte";
  import SafetySection from "./lib/sections/SafetySection.svelte";
  import AnswerProfilesSection from "./lib/sections/AnswerProfilesSection.svelte";
  import ProfilesSection from "./lib/sections/ProfilesSection.svelte";
  import Banner from "./lib/Banner.svelte";
  import { asDiscovery, type Discovery } from "./lib/sidecar";
  import { commandTransport as deckTransport } from "./lib/deckClient";
  import {
    commandTransport as cfgTransport,
    parseConfig,
    parseValidate,
    parseActiveChanged,
    toWriteBody,
    orphanedSecrets,
    referencedTokenEnvs,
    type ConfigPayload,
  } from "./lib/configClient";

  const SECTIONS = [
    "Servers", "Deck", "View", "Theme", "Macros",
    "Start profiles", "Notifications", "Safety", "Answer profiles", "Profiles",
  ];

  let discovery = $state<Discovery | null>(null);
  let payload = $state<ConfigPayload | null>(null);
  let active = $state("Servers");
  let dirty = $state(false);
  let errors = $state<string[]>([]);
  let busy = $state(false);
  // A structured status banner (replaces the old plain `notice` string). Task 7
  // reuses the optional action for the orphaned-keychain-secret cleanup.
  type BannerState = { kind: "warning" | "error" | "success"; message: string; actionLabel?: string; onAction?: () => void };
  let banner = $state<BannerState | null>(null);
  function setBanner(kind: BannerState["kind"], message: string, actionLabel?: string, onAction?: () => void): void {
    banner = { kind, message, actionLabel, onAction };
  }
  let reloadRev = $state(0); // bumps on every load(); map sections re-seed local rows on change

  const cfg = cfgTransport((cmd, args) => invoke(cmd, args));
  const preview = $derived(discovery ? deckTransport((cmd, args) => invoke(cmd, args)) : null);
  const profileOptions = $derived(payload ? ["default", ...Object.keys(payload.profiles)] : ["default"]);

  const DEFAULT_LABEL = "default (báze)";
  const optionLabel = (name: string): string => (name === "default" ? DEFAULT_LABEL : name);
  const activeValue = $derived(payload?.activeProfile ?? "default");
  const switcherDisabled = $derived(payload == null || payload.envLocked || dirty);

  // The profile whose OVERLAY the per-section editors edit. "default" → base mode. As of
  // řez β2 every _OVERLAY_SECTION (Deck/View/Theme/Safety/Macros/Start/Notifications/Answer)
  // is overlay-aware; Servers (base server list) and Profiles (meta-section) stay base-only
  // by design (not per-section overlays), so no base-only warning is needed anymore.
  const editProfile = $derived(payload && payload.activeProfile !== "default" ? payload.activeProfile : null);

  async function switchProfile(name: string): Promise<void> {
    if (!payload) return;
    if (name === payload.activeProfile) return; // no-op: same profile
    try {
      const changed = parseActiveChanged(await cfg.setActive(name));
      if (changed) {
        await load(); // re-read saved state; preview refreshes via its own poll
      } else {
        setBanner("warning", `profil '${name}' nelze aktivovat (zamčen nebo neznámý)`);
      }
    } catch (e) {
      setBanner("error", `přepnutí profilu selhalo: ${String(e)}`);
    }
  }

  async function load(): Promise<void> {
    try {
      const fresh = parseConfig(await cfg.read());
      if (fresh == null) {
        setBanner("warning", "neočekávaná odpověď configu ze sidecaru");
        return;
      }
      payload = fresh;
      dirty = false;
      errors = [];
      banner = null;
      reloadRev += 1;
    } catch {
      setBanner(
        "warning",
        payload == null
          ? "sidecar zatím neběží — zkouším znovu…"
          : "obnovení configu ze sidecaru selhalo (neuložené změny zůstávají)",
      );
    }
  }

  function markDirty(): void {
    dirty = true;
  }

  async function apply(): Promise<void> {
    if (!payload) return;
    busy = true;
    try {
      const res = parseValidate(await cfg.write(toWriteBody(payload)));
      errors = res;
      if (res.length === 0) {
        // Capture orphans from the EDITED pre-reload payload (see Design note): the reloaded
        // payload.secrets only carries still-referenced token_envs, so a renamed/deleted old
        // key would vanish and post-load detection would miss it.
        const orphans = orphanedSecrets(payload);
        dirty = false;
        await load(); // re-read saved state (preview refreshes itself via its own poll)
        if (orphans.length > 0) {
          setBanner(
            "warning",
            `${orphans.length} osiřelých keychain klíčů (${orphans.join(", ")})`,
            "uklidit",
            () => void cleanupOrphans(orphans),
          );
        }
      }
    } catch (e) {
      errors = [String(e)];
    } finally {
      busy = false;
    }
  }

  async function cleanupOrphans(names: string[]): Promise<void> {
    if (!payload) return;
    // Re-check NOW: a dirty edit after the banner appeared may have reintroduced one of these
    // token_env names. Never clear a keychain secret the current config references.
    const referenced = referencedTokenEnvs(payload);
    const secrets = { ...payload.secrets };
    for (const name of names) {
      if (referenced.has(name)) continue;
      const code = await cfg.clearSecret(name);
      if (code === 204) secrets[name] = { set: false, source: null };
      else { setBanner("error", `úklid tokenu '${name}' selhal (HTTP ${code})`); return; }
    }
    payload = { ...payload, secrets };
    setBanner("success", "osiřelé keychain klíče uklizeny");
  }

  async function discard(): Promise<void> {
    await load();
  }

  onMount(() => {
    let alive = true;
    let unlisten: (() => void) | null = null;
    void listen<Discovery>("discovery", (ev) => {
      const d = asDiscovery(ev.payload);
      if (d) discovery = d;
    }).then((fn) => {
      unlisten = fn;
    });
    void (async () => {
      while (alive && !discovery) {
        try {
          const d = asDiscovery(await invoke("get_discovery"));
          if (d) discovery = d;
        } catch {
          /* not ready */
        }
        if (!discovery) await new Promise((r) => setTimeout(r, 400));
      }
      await load();
    })();
    return () => {
      alive = false;
      unlisten?.();
    };
  });
</script>

<main>
  <header class="topbar">
    <label>
      Profil:
      <select
        value={activeValue}
        disabled={switcherDisabled}
        onchange={(e) => switchProfile((e.target as HTMLSelectElement).value)}
      >
        {#each profileOptions as name}<option value={name}>{optionLabel(name)}</option>{/each}
      </select>
    </label>
    {#if payload?.envLocked}
      <span class="hint">profil zamčen přes HERDECK_PROFILE</span>
    {:else if dirty}
      <span class="hint">ulož nebo zahoď změny pro přepnutí profilu</span>
    {/if}
    {#if dirty}<span class="dirty">● neuložené změny</span>{/if}
  </header>

  <div class="body">
    <nav class="sidebar">
      {#each SECTIONS as s}
        <button class:active={s === active} onclick={() => (active = s)}>{s}</button>
      {/each}
    </nav>

    <section class="form">
      {#if payload == null}
        <p class="hint">Načítám config… (nebo sidecar zatím neběží)</p>
      {:else if active === "Servers"}
        {#if (payload.base.servers == null || (payload.base.servers as unknown[]).length === 0)}
          <p class="hint">Zatím žádný server. Přidej první a klikni Apply pro vytvoření configu.</p>
        {/if}
        <ServersSection bind:payload onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else if active === "Deck"}
        <DeckSection bind:payload {editProfile} onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else if active === "View"}
        <ViewSection bind:payload {editProfile} onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else if active === "Theme"}
        <ThemeSection bind:payload {editProfile} onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else if active === "Macros"}
        <MacrosSection bind:payload {editProfile} onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else if active === "Start profiles"}
        <StartProfilesSection bind:payload {editProfile} {reloadRev} onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else if active === "Notifications"}
        <NotificationsSection bind:payload {editProfile} onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else if active === "Safety"}
        <SafetySection bind:payload {editProfile} onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else if active === "Answer profiles"}
        <AnswerProfilesSection bind:payload {editProfile} {reloadRev} onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else if active === "Profiles"}
        <ProfilesSection bind:payload onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else}
        <p class="hint">Neznámá sekce „{active}".</p>
      {/if}
    </section>

    <aside class="preview">
      <DeckView transport={preview} />
    </aside>
  </div>

  <footer class="savebar">
    <button onclick={discard} disabled={!dirty || busy}>Discard</button>
    {#if banner}<Banner kind={banner.kind} message={banner.message} actionLabel={banner.actionLabel} onAction={banner.onAction} />{/if}
    <span class="errcount" class:bad={errors.length > 0}>⚠ {errors.length} chyb</span>
    <button onclick={apply} disabled={!dirty || busy}>Apply</button>
  </footer>
</main>

<style>
  :global(html, body) { margin: 0; background: #0b0b0d; color: #e8e8ea; font: 13px system-ui; }
  main { display: flex; flex-direction: column; height: 100vh; }
  .topbar { display: flex; align-items: center; gap: 12px; padding: 8px 12px; border-bottom: 1px solid #222; }
  .dirty { color: #e0a030; margin-left: auto; }
  .body { flex: 1; display: grid; grid-template-columns: 160px 1fr 220px; min-height: 0; }
  .sidebar { display: flex; flex-direction: column; border-right: 1px solid #222; overflow: auto; }
  .sidebar button { text-align: left; background: none; border: 0; color: inherit; padding: 8px 12px; cursor: pointer; }
  .sidebar button.active { background: #1b1b1f; }
  .form { padding: 16px; overflow: auto; }
  .preview { border-left: 1px solid #222; padding: 8px; overflow: auto; }
  .hint { color: #888; }
  .savebar { display: flex; align-items: center; gap: 12px; padding: 8px 12px; border-top: 1px solid #222; }
  .savebar button { margin: 0; }
  .errcount { margin-left: auto; color: #888; }
  .errcount.bad { color: #e05050; }
</style>
