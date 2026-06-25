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
  import { asDiscovery, type Discovery } from "./lib/sidecar";
  import { commandTransport as deckTransport } from "./lib/deckClient";
  import {
    commandTransport as cfgTransport,
    parseConfig,
    parseValidate,
    toWriteBody,
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
  let notice = $state(""); // transient out-of-band message (e.g. a failed secret op)
  let reloadRev = $state(0); // bumps on every load(); map sections re-seed local rows on change

  const cfg = cfgTransport((cmd, args) => invoke(cmd, args));
  const preview = $derived(discovery ? deckTransport((cmd, args) => invoke(cmd, args)) : null);
  const profiles = $derived(payload ? ["default (báze)", ...Object.keys(payload.profiles)] : ["default (báze)"]);

  async function load(): Promise<void> {
    try {
      payload = parseConfig(await cfg.read());
      dirty = false;
      errors = [];
      reloadRev += 1;
    } catch {
      payload = null; // sidecar not ready / no config -> onboarding handled in řez 4
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
        dirty = false;
        await load(); // re-read saved state (preview refreshes itself via its own poll)
      }
    } catch (e) {
      errors = [String(e)];
    } finally {
      busy = false;
    }
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
      <select disabled>
        {#each profiles as p}<option>{p}</option>{/each}
      </select>
    </label>
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
        <ServersSection bind:payload onChange={markDirty} onError={(m) => (notice = m)} />
      {:else if active === "Deck"}
        <DeckSection bind:payload onChange={markDirty} onError={(m) => (notice = m)} />
      {:else if active === "View"}
        <ViewSection bind:payload onChange={markDirty} onError={(m) => (notice = m)} />
      {:else if active === "Theme"}
        <ThemeSection bind:payload onChange={markDirty} onError={(m) => (notice = m)} />
      {:else if active === "Macros"}
        <MacrosSection bind:payload onChange={markDirty} onError={(m) => (notice = m)} />
      {:else if active === "Start profiles"}
        <StartProfilesSection bind:payload {reloadRev} onChange={markDirty} onError={(m) => (notice = m)} />
      {:else if active === "Notifications"}
        <NotificationsSection bind:payload onChange={markDirty} onError={(m) => (notice = m)} />
      {:else if active === "Safety"}
        <SafetySection bind:payload onChange={markDirty} onError={(m) => (notice = m)} />
      {:else if active === "Answer profiles"}
        <AnswerProfilesSection bind:payload {reloadRev} onChange={markDirty} onError={(m) => (notice = m)} />
      {:else}
        <p class="hint">Sekce „{active}" — řez 4b.</p>
      {/if}
    </section>

    <aside class="preview">
      <DeckView transport={preview} />
    </aside>
  </div>

  <footer class="savebar">
    <button onclick={discard} disabled={!dirty || busy}>Discard</button>
    {#if notice}<span class="notice">{notice}</span>{/if}
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
  .notice { color: #e0a030; }
  .errcount { margin-left: auto; color: #888; }
  .errcount.bad { color: #e05050; }
</style>
