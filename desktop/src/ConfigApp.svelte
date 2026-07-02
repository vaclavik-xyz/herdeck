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
import UsageSection from "./lib/sections/UsageSection.svelte";
  import AnswerProfilesSection from "./lib/sections/AnswerProfilesSection.svelte";
  import ProfilesSection from "./lib/sections/ProfilesSection.svelte";
  import DesktopSection from "./lib/sections/DesktopSection.svelte";
  import Banner from "./lib/Banner.svelte";
  import { asDiscovery, type Discovery } from "./lib/sidecar";
  import { commandTransport as deckTransport } from "./lib/deckClient";
  import { defineMessages, fmt, langOf, locale, setLang } from "./lib/i18n.svelte";
  import {
    commandTransport as cfgTransport,
    effectiveLanguage,
    parseConfig,
    parseValidate,
    parseActiveChanged,
    toWriteBody,
    orphanedSecrets,
    referencedTokenEnvs,
    errorCountLabel,
    isStaleRevisionError,
    type ConfigPayload,
  } from "./lib/configClient";

  const LM = defineMessages({
    en: {
      "sec.servers": "Servers",
      "sec.deck": "Deck",
      "sec.view": "View",
      "sec.theme": "Colors",
      "sec.macros": "Macros",
      "sec.start_profiles": "Agent launchers",
      "sec.notifications": "Notifications",
      "sec.safety": "Safety",
      "sec.usage": "Usage limits",
      "sec.answer_profiles": "Answer profiles",
      "sec.profiles": "Profiles",
      "sec.desktop": "Window",
      profile: "Profile:",
      default_base: "default (base)",
      env_locked: "profile locked via HERDECK_PROFILE",
      save_to_switch: "save or discard changes to switch profiles",
      unsaved: "● unsaved changes",
      loading: "Loading config… (or the sidecar is not up yet)",
      no_servers: "No servers yet. Add the first one and hit Apply to create the config.",
      unknown_section: "Unknown section „{name}\u201c.",
      discard: "Discard",
      discard_title: "Drop unsaved changes and return to the saved config",
      apply: "Apply",
      apply_title: "Save the config and push it to the running deck",
      errlist_title: "Show or hide the error list",
      switch_failed_locked: "profile '{name}' cannot be activated (locked or unknown)",
      switch_failed: "profile switch failed: {e}",
      bad_config_reply: "unexpected config reply from the sidecar",
      sidecar_not_up: "sidecar not up yet — retrying…",
      refresh_failed: "config refresh from the sidecar failed (unsaved changes kept)",
      stale_on_disk: "the config changed on disk — load the new version (unsaved changes will be lost)",
      reload: "load",
      orphans: "{n} orphaned keychain keys ({list})",
      cleanup: "clean up",
      saved: "saved",
      cleanup_failed: "cleaning token '{name}' failed (HTTP {code})",
      orphans_cleaned: "orphaned keychain keys cleaned",
    },
    cs: {
      "sec.servers": "Servery",
      "sec.deck": "Deck",
      "sec.view": "Zobrazení",
      "sec.theme": "Barvy",
      "sec.macros": "Makra",
      "sec.start_profiles": "Spouštěče agentů",
      "sec.notifications": "Notifikace",
      "sec.safety": "Bezpečnost",
      "sec.usage": "Limity využití",
      "sec.answer_profiles": "Profily odpovědí",
      "sec.profiles": "Profily",
      "sec.desktop": "Okno",
      profile: "Profil:",
      default_base: "default (báze)",
      env_locked: "profil zamčen přes HERDECK_PROFILE",
      save_to_switch: "ulož nebo zahoď změny pro přepnutí profilu",
      unsaved: "● neuložené změny",
      loading: "Načítám config… (nebo sidecar zatím neběží)",
      no_servers: "Zatím žádný server. Přidej první a klikni Použít pro vytvoření configu.",
      unknown_section: "Neznámá sekce „{name}\u201c.",
      discard: "Zahodit",
      discard_title: "Zahodit neuložené změny a vrátit se k uloženému configu",
      apply: "Použít",
      apply_title: "Uložit config a hned ho promítnout do běžícího decku",
      errlist_title: "Zobrazit nebo skrýt seznam chyb",
      switch_failed_locked: "profil '{name}' nelze aktivovat (zamčen nebo neznámý)",
      switch_failed: "přepnutí profilu selhalo: {e}",
      bad_config_reply: "neočekávaná odpověď configu ze sidecaru",
      sidecar_not_up: "sidecar zatím neběží — zkouším znovu…",
      refresh_failed: "obnovení configu ze sidecaru selhalo (neuložené změny zůstávají)",
      stale_on_disk: "config se mezitím změnil na disku — načti novou verzi (neuložené změny se ztratí)",
      reload: "načíst",
      orphans: "{n} osiřelých keychain klíčů ({list})",
      cleanup: "uklidit",
      saved: "uloženo",
      cleanup_failed: "úklid tokenu '{name}' selhal (HTTP {code})",
      orphans_cleaned: "osiřelé keychain klíče uklizeny",
    },
  });
  const lm = $derived(LM[locale.lang]);

  // key = stable identifier (matches backend tile_sections keys where applicable),
  // label = what the sidebar shows in the CURRENT language.
  const SECTION_KEYS = [
    "servers", "deck", "view", "theme", "macros", "start_profiles",
    "notifications", "safety", "usage", "answer_profiles", "profiles", "desktop",
  ] as const;
  const SECTIONS = $derived(SECTION_KEYS.map((key) => ({ key, label: lm[`sec.${key}`] })));

  // klik-to-jump: backend tile section KEY (from deckClient /state.tile_sections) maps
  // 1:1 onto sidebar keys. A preview tile click switches `active` to its section.
  const JUMPABLE = new Set(["view", "start_profiles", "answer_profiles", "profiles"]);
  function jumpToSection(key: string): void {
    if (JUMPABLE.has(key)) active = key;
  }

  let discovery = $state<Discovery | null>(null);
  let payload = $state<ConfigPayload | null>(null);
  let active = $state("servers");
  let dirty = $state(false);
  let errors = $state<string[]>([]);
  let showErrors = $state(false); // expanded error list above the savebar
  let busy = $state(false);
  // A structured status banner (replaces the old plain `notice` string). Task 7
  // reuses the optional action for the orphaned-keychain-secret cleanup.
  type BannerState = { kind: "warning" | "error" | "success"; message: string; actionLabel?: string; onAction?: () => void };
  let banner = $state<BannerState | null>(null);
  function setBanner(kind: BannerState["kind"], message: string, actionLabel?: string, onAction?: () => void): void {
    banner = { kind, message, actionLabel, onAction };
  }
  let reloadRev = $state(0); // bumps on every load(); map sections re-seed local rows on change

  // The editor speaks the config's EFFECTIVE [view].language (active profile
  // override → extends chain → base) — including LIVE while the user flips the
  // select, before Apply (instant preview of the UI language).
  $effect(() => {
    if (payload != null) setLang(langOf(effectiveLanguage(payload)));
  });

  const cfg = cfgTransport((cmd, args) => invoke(cmd, args));
  const preview = $derived(discovery ? deckTransport((cmd, args) => invoke(cmd, args)) : null);
  const profileOptions = $derived(payload ? ["default", ...Object.keys(payload.profiles)] : ["default"]);

  const optionLabel = (name: string): string => (name === "default" ? lm.default_base : name);
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
        setBanner("warning", fmt(lm.switch_failed_locked, { name }));
      }
    } catch (e) {
      setBanner("error", fmt(lm.switch_failed, { e: String(e) }));
    }
  }

  async function load(): Promise<void> {
    try {
      const fresh = parseConfig(await cfg.read());
      if (fresh == null) {
        setBanner("warning", lm.bad_config_reply);
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
        payload == null ? lm.sidecar_not_up : lm.refresh_failed,
      );
    }
  }

  // Live validation: the backend channel (POST /config/validate) existed but
  // nothing called it — every mistake surfaced only at Apply. Debounced so a
  // burst of keystrokes costs one request; results feed the same errors badge
  // + expandable list the Apply path uses.
  let validateTimer: ReturnType<typeof setTimeout> | undefined;

  function markDirty(): void {
    dirty = true;
    if (validateTimer) clearTimeout(validateTimer);
    validateTimer = setTimeout(() => void liveValidate(), 500);
  }

  async function liveValidate(): Promise<void> {
    if (!payload || !dirty) return;
    try {
      errors = parseValidate(await cfg.validate(toWriteBody(payload)));
    } catch {
      /* sidecar hiccup — keep the previous result; Apply still validates */
    }
  }

  async function apply(): Promise<void> {
    if (!payload) return;
    busy = true;
    try {
      const res = parseValidate(await cfg.write(toWriteBody(payload)));
      if (res.some(isStaleRevisionError)) {
        // The files changed under the editor (re-onboarding, tray switch, hand
        // edit): never resurrect the stale snapshot — offer a reload instead.
        errors = [];
        setBanner(
          "warning",
          lm.stale_on_disk,
          lm.reload,
          () => void load(),
        );
        return;
      }
      errors = res;
      if (res.length === 0) {
        showErrors = false;
        // Capture orphans from the EDITED pre-reload payload (see Design note): the reloaded
        // payload.secrets only carries still-referenced token_envs, so a renamed/deleted old
        // key would vanish and post-load detection would miss it.
        const orphans = orphanedSecrets(payload);
        dirty = false;
        await load(); // re-read saved state (preview refreshes itself via its own poll)
        // A changed [hotkeys] accelerator only takes effect once Rust re-registers it.
        void invoke("reload_hotkey").catch(() => {});
        if (orphans.length > 0) {
          setBanner(
            "warning",
            fmt(lm.orphans, { n: orphans.length, list: orphans.join(", ") }),
            lm.cleanup,
            () => void cleanupOrphans(orphans),
          );
        } else if (banner == null) {
          // load() surfaces its own warning on a failed refresh — never mask it
          setBanner("success", lm.saved);
        }
      } else {
        // A rejected Apply must SHOW what is wrong, not just count it.
        showErrors = true;
        banner = null;
      }
    } catch (e) {
      errors = [String(e)];
      showErrors = true;
      banner = null;
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
      else { setBanner("error", fmt(lm.cleanup_failed, { name, code })); return; }
    }
    payload = { ...payload, secrets };
    setBanner("success", lm.orphans_cleaned);
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
    // The config window is hidden on close, not destroyed — a payload can be
    // days old when it reappears. Refresh a CLEAN editor on visibility.
    const onVisible = (): void => {
      if (!document.hidden && payload != null && !dirty) void load();
    };
    document.addEventListener("visibilitychange", onVisible);
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
      if (validateTimer) clearTimeout(validateTimer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  });
</script>

<main>
  <header class="topbar">
    <label>
      {lm.profile}
      <select
        value={activeValue}
        disabled={switcherDisabled}
        onchange={(e) => switchProfile((e.target as HTMLSelectElement).value)}
      >
        {#each profileOptions as name}<option value={name}>{optionLabel(name)}</option>{/each}
      </select>
    </label>
    {#if payload?.envLocked}
      <span class="hint">{lm.env_locked}</span>
    {:else if dirty}
      <span class="hint">{lm.save_to_switch}</span>
    {/if}
    {#if dirty}
      <span class="dirty" class:bad={errors.length > 0}>
        {lm.unsaved}{errors.length > 0 ? ` · ${errorCountLabel(errors.length, locale.lang)}` : ""}
      </span>
    {/if}
  </header>

  <div class="body">
    <nav class="sidebar">
      {#each SECTIONS as s}
        <button class:active={s.key === active} onclick={() => (active = s.key)}>{s.label}</button>
      {/each}
    </nav>

    <section class="form">
      {#if payload == null}
        <p class="hint">{lm.loading}</p>
      {:else if active === "servers"}
        {#if (payload.base.servers == null || (payload.base.servers as unknown[]).length === 0)}
          <p class="hint">{lm.no_servers}</p>
        {/if}
        <ServersSection bind:payload onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else if active === "deck"}
        <DeckSection bind:payload {editProfile} onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else if active === "view"}
        <ViewSection bind:payload {editProfile} onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else if active === "theme"}
        <ThemeSection bind:payload {editProfile} onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else if active === "macros"}
        <MacrosSection bind:payload {editProfile} onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else if active === "start_profiles"}
        <StartProfilesSection bind:payload {editProfile} {reloadRev} onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else if active === "notifications"}
        <NotificationsSection bind:payload {editProfile} onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else if active === "safety"}
        <SafetySection bind:payload {editProfile} onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else if active === "usage"}
        <UsageSection bind:payload {editProfile} onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else if active === "answer_profiles"}
        <AnswerProfilesSection bind:payload {editProfile} {reloadRev} onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else if active === "profiles"}
        <ProfilesSection bind:payload onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else if active === "desktop"}
        <DesktopSection bind:payload onChange={markDirty} onError={(m) => setBanner("error", m)} />
      {:else}
        <p class="hint">{fmt(lm.unknown_section, { name: active })}</p>
      {/if}
    </section>

    <aside class="preview">
      <DeckView transport={preview} onJump={jumpToSection} />
    </aside>
  </div>

  {#if showErrors && errors.length > 0}
    <div class="errlist" role="alert">
      <ul>
        {#each errors as err}<li>{err}</li>{/each}
      </ul>
    </div>
  {/if}

  <footer class="savebar">
    <button onclick={discard} disabled={!dirty || busy} title={lm.discard_title}>{lm.discard}</button>
    {#if banner}<Banner kind={banner.kind} message={banner.message} actionLabel={banner.actionLabel} onAction={banner.onAction} />{/if}
    <span class="spacer"></span>
    {#if errors.length > 0}
      <button class="errcount" title={lm.errlist_title} onclick={() => (showErrors = !showErrors)}>
        ⚠ {errorCountLabel(errors.length, locale.lang)} {showErrors ? "▾" : "▸"}
      </button>
    {/if}
    <button onclick={apply} disabled={!dirty || busy} title={lm.apply_title}>{lm.apply}</button>
  </footer>
</main>

<style>
  /* color-scheme keeps NATIVE widgets (selects + their popup menus, checkboxes,
     number spinners, scrollbars) dark — without it WebKit renders them in light
     mode against the dark theme. */
  :global(html, body) { margin: 0; background: #0b0b0d; color: #e8e8ea; font: 13px system-ui; color-scheme: dark; accent-color: #2563eb; }
  main { display: flex; flex-direction: column; height: 100vh; }
  .topbar { display: flex; align-items: center; gap: 12px; padding: 8px 12px; border-bottom: 1px solid #222; }
  .topbar select { background: #1b1b1f; color: #e8e8ea; border: 1px solid #2a2a2e; border-radius: 6px; padding: 3px 6px; font: inherit; }
  .dirty { color: #e0a030; margin-left: auto; }
  .dirty.bad { color: #e05050; }
  .body { flex: 1; display: grid; grid-template-columns: 160px 1fr 220px; min-height: 0; }
  .sidebar { display: flex; flex-direction: column; border-right: 1px solid #222; overflow: auto; }
  .sidebar button { text-align: left; background: none; border: 0; color: inherit; padding: 8px 12px; cursor: pointer; }
  .sidebar button.active { background: #1b1b1f; }
  .form { padding: 16px; overflow: auto; }
  .preview { border-left: 1px solid #222; padding: 8px; overflow: auto; }
  .hint { color: #888; }
  .savebar { display: flex; align-items: center; gap: 12px; padding: 8px 12px; border-top: 1px solid #222; }
  .savebar button { margin: 0; padding: 6px 14px; border: 1px solid #2a2a2e; border-radius: 7px; background: #1b1b1f; color: #e8e8ea; font: inherit; cursor: pointer; }
  .savebar button:disabled { opacity: 0.5; cursor: default; }
  .spacer { flex: 1; }
  .errcount { background: none; border: 0; cursor: pointer; color: #e05050; }
  .errlist { border-top: 1px solid #3a1d1d; background: #171012; color: #e08080; max-height: 120px; overflow: auto; padding: 6px 12px; font-size: 12px; }
  .errlist ul { margin: 0; padding-left: 18px; }
  .errlist li { margin: 2px 0; }
</style>
