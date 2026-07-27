<script lang="ts">
  import { onMount, setContext, tick } from "svelte";
  import { writable } from "svelte/store";
  import { invoke } from "@tauri-apps/api/core";
  import { listen } from "@tauri-apps/api/event";
  import ArrowBendDownRight from "phosphor-svelte/lib/ArrowBendDownRight";
  import ArrowRight from "phosphor-svelte/lib/ArrowRight";
  import BellSimple from "phosphor-svelte/lib/BellSimple";
  import Command from "phosphor-svelte/lib/Command";
  import Gauge from "phosphor-svelte/lib/Gauge";
  import GridFour from "phosphor-svelte/lib/GridFour";
  import HouseSimple from "phosphor-svelte/lib/HouseSimple";
  import Layout from "phosphor-svelte/lib/Layout";
  import MagnifyingGlass from "phosphor-svelte/lib/MagnifyingGlass";
  import Palette from "phosphor-svelte/lib/Palette";
  import PlugsConnected from "phosphor-svelte/lib/PlugsConnected";
  import ShieldCheck from "phosphor-svelte/lib/ShieldCheck";
  import SlidersHorizontal from "phosphor-svelte/lib/SlidersHorizontal";
  import StackSimple from "phosphor-svelte/lib/StackSimple";
  import TerminalWindow from "phosphor-svelte/lib/TerminalWindow";
  import DeckView from "./lib/DeckView.svelte";
  import StatusRibbon from "./lib/StatusRibbon.svelte";
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
  import {
    commandTransport as deckTransport,
    initialView,
    parseState,
    type DeckViewModel,
  } from "./lib/deckClient";
  import { visibilityGatedLoop } from "./lib/pollGate";
  import { connectionInventory, type ConnectionHealth } from "./lib/connectionStatus";
  import { filterSettingsNavigation } from "./lib/settingsNavigation";
  import { FIELD_VALIDATION_CONTEXT } from "./lib/validationContext";
  import {
    classifyValidationErrors,
    messagesForSection,
    revealValidationField,
    type ValidationIssue,
  } from "./lib/validationIssues";
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
    effectiveActiveServerIds,
    isStaleRevisionError,
    serversOf,
    type ConfigPayload,
  } from "./lib/configClient";

  let { interactive = true }: { interactive?: boolean } = $props();

  const LM = defineMessages({
    en: {
      "sec.overview": "Overview",
      "sec.servers": "Connections",
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
      "group.control": "Control",
      "group.deck": "Deck",
      "group.agents": "Agents",
      "group.policies": "Policies",
      "group.system": "System",
      "desc.servers": "Choose which local sessions and remote bridges feed the deck, then verify their live state.",
      "desc.deck": "Select the deck hardware, grid, simulator, and machine-specific timing.",
      "desc.view": "Control what agent tiles show and how launchers fit into the deck.",
      "desc.theme": "Assign stable colors to agent states and individual servers.",
      "desc.macros": "Create short messages that can be sent to a running agent with one press.",
      "desc.start_profiles": "Define the commands available when starting a new agent from the deck.",
      "desc.notifications": "Choose when Herdeck alerts you and whether macOS or Telegram delivers those alerts.",
      "desc.safety": "Require deliberate confirmation for destructive or persistent agent actions.",
      "desc.usage": "Select the account limits shown on the deck and how often they refresh.",
      "desc.answer_profiles": "Map deck actions to the key sequences expected by each agent tool.",
      "desc.profiles": "Compose named working contexts from server selections and inherited settings.",
      "desc.desktop": "Set window behavior and the global shortcut that shows or hides the deck.",
      search_settings: "Search settings",
      clear_search: "Clear search",
      no_search_results: "No settings match this search.",
      editing_profile: "Editing profile: {name}",
      overview_eyebrow: "System overview",
      overview_title: "System status",
      overview_ready: "Runtime, sessions, and notifications are responding.",
      overview_connecting: "Waiting for the local Herdeck runtime.",
      live_deck: "Live deck",
      live_deck_hint: "Select a key to open its related settings.",
      runtime: "Runtime",
      ready: "Ready",
      connecting: "Connecting",
      agents: "Agents",
      working: "Working",
      blocked: "Blocked",
      done: "Done",
      waiting: "Waiting",
      connections: "Connections",
      configured: "configured",
      connected: "Connected",
      disconnected: "Disconnected",
      unavailable: "Unavailable",
      inactive: "Not selected",
      connection_status: "Connection status",
      live_now: "live now",
      local_inventory: "Local sessions",
      local_inventory_hint: "Sessions discovered on this Mac. Selected sessions are connected automatically.",
      remote_inventory: "Remote bridges",
      remote_inventory_hint: "Servers selected by the active profile are connected automatically.",
      active_profile: "Active profile",
      inactive_profile: "Not used by this profile",
      runtime_identity: "runtime",
      socket_identity: "socket",
      no_local_sessions: "No Herdr sessions were discovered on this Mac.",
      no_remote_servers: "No remote bridges are configured.",
      new_server: "New bridge",
      local_sessions: "Local sessions",
      remote_servers: "Remote servers",
      deck_device: "Deck device",
      automatic: "Automatic",
      settings_eyebrow: "Settings",
      settings_hint: "Changes are validated live and applied to the running deck.",
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
      sidecar_not_up: "sidecar not up yet. Retrying…",
      refresh_failed: "config refresh from the sidecar failed (unsaved changes kept)",
      stale_on_disk: "the config changed on disk. Load the new version (unsaved changes will be lost)",
      reload: "load",
      orphans: "{n} orphaned keychain keys ({list})",
      cleanup: "clean up",
      saved: "saved",
      cleanup_failed: "cleaning token '{name}' failed (HTTP {code})",
      orphans_cleaned: "orphaned keychain keys cleaned",
    },
    cs: {
      "sec.overview": "Přehled",
      "sec.servers": "Připojení",
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
      "group.control": "Ovládání",
      "group.deck": "Deck",
      "group.agents": "Agenti",
      "group.policies": "Pravidla",
      "group.system": "Systém",
      "desc.servers": "Vyber lokální sessions a vzdálené bridges pro deck a ověř jejich skutečný stav.",
      "desc.deck": "Nastav hardware decku, mřížku, simulátor a časování pro tento počítač.",
      "desc.view": "Urči obsah dlaždic agentů a umístění spouštěčů na decku.",
      "desc.theme": "Přiřaď stálé barvy stavům agentů a jednotlivým serverům.",
      "desc.macros": "Vytvoř krátké zprávy, které odešleš běžícímu agentovi jedním stiskem.",
      "desc.start_profiles": "Definuj příkazy dostupné při spuštění nového agenta z decku.",
      "desc.notifications": "Vyber, kdy tě Herdeck upozorní a zda oznámení doručí macOS nebo Telegram.",
      "desc.safety": "Vyžádej vědomé potvrzení destruktivních nebo trvalých akcí agenta.",
      "desc.usage": "Vyber limity účtů zobrazené na decku a interval jejich obnovy.",
      "desc.answer_profiles": "Namapuj akce decku na klávesy očekávané jednotlivými nástroji agentů.",
      "desc.profiles": "Sestav pojmenované pracovní kontexty z výběru serverů a zděděných nastavení.",
      "desc.desktop": "Nastav chování okna a globální zkratku pro zobrazení nebo skrytí decku.",
      search_settings: "Hledat nastavení",
      clear_search: "Vymazat hledání",
      no_search_results: "Žádné nastavení tomuto hledání neodpovídá.",
      editing_profile: "Upravuješ profil: {name}",
      overview_eyebrow: "Přehled systému",
      overview_title: "Stav systému",
      overview_ready: "Runtime, sessions a notifikace odpovídají.",
      overview_connecting: "Čekám na lokální Herdeck runtime.",
      live_deck: "Živý deck",
      live_deck_hint: "Výběrem tlačítka otevřeš související nastavení.",
      runtime: "Runtime",
      ready: "Připraven",
      connecting: "Připojuji",
      agents: "Agenti",
      working: "Pracují",
      blocked: "Blokováni",
      done: "Hotovo",
      waiting: "Čeká",
      connections: "Připojení",
      configured: "nastaveno",
      connected: "Připojeno",
      disconnected: "Odpojeno",
      unavailable: "Nedostupné",
      inactive: "Nevybráno",
      connection_status: "Stav připojení",
      live_now: "právě připojeno",
      local_inventory: "Lokální sessions",
      local_inventory_hint: "Sessions nalezené na tomto Macu. Vybrané sessions se připojují automaticky.",
      remote_inventory: "Vzdálené bridges",
      remote_inventory_hint: "Servery vybrané aktivním profilem se připojují automaticky.",
      active_profile: "Aktivní profil",
      inactive_profile: "Tento profil nepoužívá",
      runtime_identity: "runtime",
      socket_identity: "socket",
      no_local_sessions: "Na tomto Macu nebyla nalezena žádná Herdr session.",
      no_remote_servers: "Není nastavený žádný vzdálený bridge.",
      new_server: "Nový bridge",
      local_sessions: "Lokální sessions",
      remote_servers: "Vzdálené servery",
      deck_device: "Zařízení decku",
      automatic: "Automaticky",
      settings_eyebrow: "Nastavení",
      settings_hint: "Změny se průběžně ověřují a po uložení se promítnou do běžícího decku.",
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
      sidecar_not_up: "sidecar zatím neběží. Zkouším znovu…",
      refresh_failed: "obnovení configu ze sidecaru selhalo (neuložené změny zůstávají)",
      stale_on_disk: "config se mezitím změnil na disku. Načti novou verzi (neuložené změny se ztratí)",
      reload: "načíst",
      orphans: "{n} osiřelých keychain klíčů ({list})",
      cleanup: "uklidit",
      saved: "uloženo",
      cleanup_failed: "úklid tokenu '{name}' selhal (HTTP {code})",
      orphans_cleaned: "osiřelé keychain klíče uklizeny",
    },
  });
  const lm = $derived(LM[locale.lang]);
  const fieldValidationMessages = writable<Record<string, string[]>>({});
  setContext(FIELD_VALIDATION_CONTEXT, fieldValidationMessages);
  const browserMode = typeof window !== "undefined" && !("__TAURI_INTERNALS__" in window);
  const browserSection = browserMode ? new URLSearchParams(window.location.search).get("section") : null;

  // key = stable identifier (matches backend tile_sections keys where applicable),
  // label = what the sidebar shows in the CURRENT language.
  const NAV_GROUPS = $derived([
    { label: lm["group.control"], items: [
      { key: "overview", icon: HouseSimple, label: lm["sec.overview"] },
      { key: "servers", icon: PlugsConnected, label: lm["sec.servers"] },
    ] },
    { label: lm["group.deck"], items: [
      { key: "deck", icon: GridFour, label: lm["sec.deck"] },
      { key: "view", icon: Layout, label: lm["sec.view"] },
      { key: "theme", icon: Palette, label: lm["sec.theme"] },
    ] },
    { label: lm["group.agents"], items: [
      { key: "start_profiles", icon: TerminalWindow, label: lm["sec.start_profiles"] },
      { key: "answer_profiles", icon: ArrowBendDownRight, label: lm["sec.answer_profiles"] },
      { key: "macros", icon: Command, label: lm["sec.macros"] },
    ] },
    { label: lm["group.policies"], items: [
      { key: "notifications", icon: BellSimple, label: lm["sec.notifications"] },
      { key: "safety", icon: ShieldCheck, label: lm["sec.safety"] },
      { key: "usage", icon: Gauge, label: lm["sec.usage"] },
    ] },
    { label: lm["group.system"], items: [
      { key: "profiles", icon: StackSimple, label: lm["sec.profiles"] },
      { key: "desktop", icon: SlidersHorizontal, label: lm["sec.desktop"] },
    ] },
  ]);

  // klik-to-jump: backend tile section KEY (from deckClient /state.tile_sections) maps
  // 1:1 onto sidebar keys. A preview tile click switches `active` to its section.
  const JUMPABLE = new Set(["view", "start_profiles", "answer_profiles", "profiles"]);
  const PROFILE_SCOPED = new Set(["deck", "view", "theme", "macros", "start_profiles", "notifications", "safety", "usage", "answer_profiles"]);
  function jumpToSection(key: string): void {
    if (JUMPABLE.has(key)) active = key;
  }

  function selectSection(key: string): void {
    active = key;
    navQuery = "";
    validationEditProfile = undefined;
  }

  function searchKeydown(event: KeyboardEvent): void {
    if (event.key !== "Enter") return;
    const first = filteredNavGroups[0]?.items[0];
    if (first) selectSection(first.key);
  }

  let discovery = $state<Discovery | null>(null);
  let payload = $state<ConfigPayload | null>(null);
  // Runtime diagnostics must describe the last config accepted by the
  // sidecar, not the draft currently being edited in `payload`.
  let appliedPayload = $state<ConfigPayload | null>(null);
  let active = $state(browserSection ?? "overview");
  let navQuery = $state("");
  let navSearchInput: HTMLInputElement;
  let contentRoot: HTMLElement;
  let deckView = $state<DeckViewModel>(initialView());
  let dirty = $state(false);
  let errors = $state<string[]>([]);
  // undefined follows the runtime profile; null deliberately edits base;
  // a string opens that profile's overlay without switching the running deck.
  let validationEditProfile = $state<string | null | undefined>(undefined);
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

  const editProfile = $derived(validationEditProfile !== undefined
    ? validationEditProfile
    : payload && payload.activeProfile !== "default" ? payload.activeProfile : null);
  const showProfileContext = $derived(
    PROFILE_SCOPED.has(active) && (validationEditProfile !== undefined || editProfile != null),
  );
  const validationIssues = $derived(classifyValidationErrors(errors, payload ?? undefined));
  const validationSections = $derived(new Set(validationIssues.flatMap((entry) => entry.section ? [entry.section] : [])));
  $effect(() => fieldValidationMessages.set(messagesForSection(
    validationIssues,
    active,
    PROFILE_SCOPED.has(active) ? editProfile : null,
  )));

  async function focusValidationIssue(entry: ValidationIssue): Promise<void> {
    showErrors = true;
    if (entry.section == null || entry.fieldKey == null) return;
    validationEditProfile = PROFILE_SCOPED.has(entry.section) ? entry.profileContext : undefined;
    active = entry.section;
    navQuery = "";
    await tick();
    revealValidationField(contentRoot, entry.fieldKey, entry.owner);
  }

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
  const switcherDisabled = $derived(browserMode || payload == null || payload.envLocked || dirty);
  const activeLabel = $derived(
    NAV_GROUPS.flatMap((group) => group.items).find((item) => item.key === active)?.label ?? active,
  );
  const SECTION_DESCRIPTIONS = $derived<Record<string, string>>({
    servers: lm["desc.servers"],
    deck: lm["desc.deck"],
    view: lm["desc.view"],
    theme: lm["desc.theme"],
    macros: lm["desc.macros"],
    start_profiles: lm["desc.start_profiles"],
    notifications: lm["desc.notifications"],
    safety: lm["desc.safety"],
    usage: lm["desc.usage"],
    answer_profiles: lm["desc.answer_profiles"],
    profiles: lm["desc.profiles"],
    desktop: lm["desc.desktop"],
  });
  const activeDescription = $derived(SECTION_DESCRIPTIONS[active] ?? lm.settings_hint);
  const filteredNavGroups = $derived.by(() => {
    return filterSettingsNavigation(NAV_GROUPS, SECTION_DESCRIPTIONS, navQuery, locale.lang);
  });
  const selectedLocalSessions = $derived(appliedPayload?.localSessions.filter((session) => session.selected) ?? []);
  const connectedLocalSessions = $derived(selectedLocalSessions.filter((session) => {
    const runtimeId = deckView.localConnections[session.name];
    return runtimeId !== undefined && deckView.connections[runtimeId] === true;
  }).length);
  const activeRemoteServerIds = $derived(new Set(appliedPayload ? effectiveActiveServerIds(appliedPayload) : []));
  const remoteServerRecords = $derived(appliedPayload
    ? serversOf(appliedPayload).filter((server) => activeRemoteServerIds.has(server.id))
    : []);
  const remoteServers = $derived(remoteServerRecords.length);
  const connectedRemoteServers = $derived(remoteServerRecords.filter((server) => deckView.connections[server.id] === true).length);
  const runtimeReady = $derived(discovery != null && deckView.online);
  const connectionRows = $derived(appliedPayload ? connectionInventory(appliedPayload, deckView) : { local: [], remote: [] });

  function connectionLabel(health: ConnectionHealth): string {
    return lm[health];
  }

  function browserPreviewPayload(): ConfigPayload {
    const demo = parseConfig({
      base: {
        servers: [{ id: "macbench", url: "ws://macbench:8788", token_env: "HERDECK_TOKEN_MACBENCH" }],
        deck: { grid: "5x3", overview_order: ["local:personal", "macbench"] },
        view: { management: "launcher_menu", tile_fields: ["repo", "status"] },
        theme: { colors: {} },
        desktop: { window_mode: "normal" },
      },
      profiles: {},
      local: { local: { deck: "auto" }, hardware: { brightness: 80 } },
      local_sessions: [{ name: "Personal MBP", server_id: "local:personal", socket_path: "/tmp/herdr.sock", available: true, selected: true }],
      active_profile: "default",
      runtime_deck: "auto",
      secrets: {},
    });
    if (!demo) throw new Error("browser preview fixture is invalid");
    return demo;
  }

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
      appliedPayload = fresh;
      dirty = false;
      errors = [];
      validationEditProfile = undefined;
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
        appliedPayload = JSON.parse(JSON.stringify(payload)) as ConfigPayload;
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
        const first = classifyValidationErrors(res, payload)[0];
        if (first) await focusValidationIssue(first);
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
    const onSettingsShortcut = (event: KeyboardEvent): void => {
      if (!interactive) return;
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        navSearchInput?.focus();
      } else if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        if (!browserMode && payload && dirty && !busy) void apply();
      } else if (event.key === "Escape" && document.activeElement === navSearchInput) {
        navQuery = "";
        navSearchInput.blur();
      }
    };
    document.addEventListener("keydown", onSettingsShortcut);
    return () => document.removeEventListener("keydown", onSettingsShortcut);
  });

  onMount(() => {
    if (browserMode) {
      payload = browserPreviewPayload();
      appliedPayload = payload;
      reloadRev += 1;
      return () => {
        if (validateTimer) clearTimeout(validateTimer);
      };
    }

    let alive = true;
    let unlisten: (() => void) | null = null;
    const statusPoll = visibilityGatedLoop(
      async () => {
        const transport = preview;
        if (!transport) {
          deckView = { ...deckView, online: false, connected: false, connections: {}, localConnections: {} };
          return;
        }
        try {
          const state = parseState(await transport.fetchState());
          if (!state) throw new Error("invalid deck state");
          deckView = {
            ...deckView,
            online: true,
            slots: state.slots || deckView.slots,
            source: state.source,
            connected: state.connected,
            summary: state.summary,
            language: state.language,
            sections: state.sections,
            connections: state.connections,
            localConnections: state.localConnections,
          };
        } catch {
          deckView = { ...deckView, online: false, connected: false, connections: {}, localConnections: {} };
        }
      },
      () => 1000,
    );
    void listen<Discovery>("discovery", (ev) => {
      const d = asDiscovery(ev.payload);
      if (d) discovery = d;
    }).then((fn) => {
      unlisten = fn;
    }).catch(() => {
      /* plain-browser design preview has no Tauri event bridge */
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
      statusPoll.stop();
      unlisten?.();
      if (validateTimer) clearTimeout(validateTimer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  });
</script>

<main>
  <header class="topbar">
    <div class="brand" aria-label="Herdeck">
      <span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
      <strong>Herdeck</strong>
    </div>
    <span class="status-pill"><span class:ready={runtimeReady} class="status-dot"></span>{lm.runtime} · {runtimeReady ? lm.ready : lm.connecting}</span>
    <span class="status-pill secondary-status"><span class:ready={remoteServers > 0 && connectedRemoteServers === remoteServers} class="status-dot"></span>{connectedRemoteServers}/{remoteServers} {lm.remote_servers.toLowerCase()}</span>
    <span class="top-spacer"></span>
    <label class="profile-picker">
      <span>{lm.profile}</span>
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
      <div class="settings-search" role="search">
        <MagnifyingGlass size={12} aria-hidden="true" />
        <input bind:this={navSearchInput} bind:value={navQuery} onkeydown={searchKeydown} placeholder={lm.search_settings} aria-label={lm.search_settings} />
        {#if navQuery}<button type="button" onclick={() => (navQuery = "")} aria-label={lm.clear_search}>×</button>{:else}<kbd>⌘K</kbd>{/if}
      </div>
      {#each filteredNavGroups as group}
        <div class="nav-group">
          <span class="nav-label">{group.label}</span>
          {#each group.items as item}
            {@const Icon = item.icon}
            <button class:active={item.key === active} class:problem={validationSections.has(item.key)} onclick={() => selectSection(item.key)}>
              <span class="nav-icon" aria-hidden="true"><Icon size={14} weight="regular" /></span>
              <span>{item.label}</span>
            </button>
          {/each}
        </div>
      {/each}
      {#if filteredNavGroups.length === 0}<p class="nav-empty">{lm.no_search_results}</p>{/if}
      <div class="sidebar-version"><strong>Herdeck Desktop</strong><span>v0.1.1</span></div>
    </nav>

    <section class="content" bind:this={contentRoot}>
      {#if active === "overview"}
        <div class="page-heading">
          <div><h1>{lm.overview_title}</h1><p>{runtimeReady ? lm.overview_ready : lm.overview_connecting}</p></div>
          <button class="secondary" onclick={() => (active = "servers")}>{lm.connections}</button>
        </div>
        <StatusRibbon
          ready={runtimeReady}
          summary={deckView.summary}
          labels={{
            runtime: lm.runtime, ready: lm.ready, connecting: lm.connecting,
            agents: lm.agents, working: lm.working, blocked: lm.blocked,
            waiting: lm.waiting, done: lm.done,
          }}
        />
        <div class="overview-stage">
          <article class="card live-deck-card">
            <div class="card-heading"><div><h2>{lm.live_deck}</h2><p>{lm.live_deck_hint}</p></div><button class="secondary" onclick={() => (active = "deck")}>{lm["sec.deck"]}</button></div>
            <div class="deck-surface"><DeckView transport={preview} onJump={jumpToSection} onView={(view) => (deckView = view)} /></div>
          </article>
          <div class="overview-stack">
            <article class="card connection-card"><div class="card-heading"><div><h2>{lm.connections}</h2><p>{selectedLocalSessions.length + remoteServers} {lm.configured}</p></div><button class="icon-button" onclick={() => (active = "servers")} aria-label={lm.connections}><ArrowRight size={14} /></button></div><div class="connection-row"><span class:ready={selectedLocalSessions.length > 0 && connectedLocalSessions === selectedLocalSessions.length} class="status-dot"></span><div><strong>{lm.local_sessions}</strong><small>{connectedLocalSessions}/{selectedLocalSessions.length} {lm.ready.toLowerCase()}</small></div><span class="badge">{selectedLocalSessions.length}</span></div><div class="connection-row"><span class:ready={remoteServers > 0 && connectedRemoteServers === remoteServers} class="status-dot"></span><div><strong>{lm.remote_servers}</strong><small>{connectedRemoteServers}/{remoteServers} {lm.ready.toLowerCase()}</small></div><span class="badge">{remoteServers}</span></div><div class="connection-row"><span class:ready={deckView.online} class="status-dot"></span><div><strong>{lm.deck_device}</strong><small>{payload?.runtimeDeck ?? lm.automatic}</small></div></div></article>
          </div>
        </div>
      {:else}
        <div class="page-heading settings-heading">
          <div>
            <div class="title-line">
              <h1>{activeLabel}</h1>
              {#if showProfileContext}<span class="scope-badge">{fmt(lm.editing_profile, { name: editProfile ?? lm.default_base })}</span>{/if}
            </div>
            <p>{activeDescription}</p>
          </div>
        </div>
        {#if payload == null}
          <article class="card loading-card"><p class="hint">{lm.loading}</p></article>
        {:else if active === "servers"}
          <div class="connections-workbench">
            <section class="connection-summary" aria-labelledby="connection-summary-heading">
              <div>
                <span class="eyebrow" id="connection-summary-heading">{lm.connection_status}</span>
                <strong>{connectedLocalSessions + connectedRemoteServers}/{selectedLocalSessions.length + remoteServers}</strong>
                <small>{lm.live_now}</small>
              </div>
              <dl>
                <div><dt>{lm.runtime}</dt><dd class:connected={runtimeReady}>{runtimeReady ? lm.ready : lm.connecting}</dd></div>
                <div><dt>{lm.local_sessions}</dt><dd>{connectedLocalSessions}/{selectedLocalSessions.length}</dd></div>
                <div><dt>{lm.remote_servers}</dt><dd>{connectedRemoteServers}/{remoteServers}</dd></div>
              </dl>
            </section>

            <div class="connection-inventories">
              <article class="card diagnostic-card">
                <header>
                  <div><h2>{lm.local_inventory}</h2><p>{lm.local_inventory_hint}</p></div>
                  <span>{connectionRows.local.length}</span>
                </header>
                <div class="diagnostic-list">
                  {#each connectionRows.local as row (row.name)}
                    <div class="diagnostic-row">
                      <span class:connected={row.health === "connected"} class:unavailable={row.health === "unavailable"} class:inactive={row.health === "inactive"} class="connection-light" aria-hidden="true"></span>
                      <div class="connection-identity">
                        <strong>{row.name}</strong>
                        <small><span>{lm.socket_identity}</span>{row.socketPath}</small>
                        {#if row.runtimeId}<small><span>{lm.runtime_identity}</span>{row.runtimeId}</small>{/if}
                      </div>
                      <span class:connected={row.health === "connected"} class:unavailable={row.health === "unavailable"} class:inactive={row.health === "inactive"} class="state-label">{connectionLabel(row.health)}</span>
                    </div>
                  {:else}
                    <p class="empty-diagnostic">{lm.no_local_sessions}</p>
                  {/each}
                </div>
              </article>

              <article class="card diagnostic-card">
                <header>
                  <div><h2>{lm.remote_inventory}</h2><p>{lm.remote_inventory_hint}</p></div>
                  <span>{connectionRows.remote.length}</span>
                </header>
                <div class="diagnostic-list">
                  {#each connectionRows.remote as row, index (`${row.id}:${index}`)}
                    <div class="diagnostic-row">
                      <span class:connected={row.health === "connected"} class:inactive={row.health === "inactive"} class="connection-light" aria-hidden="true"></span>
                      <div class="connection-identity">
                        <strong>{row.id || lm.new_server}</strong>
                        <small>{row.url}</small>
                        <small class="profile-use">{row.active ? lm.active_profile : lm.inactive_profile}</small>
                      </div>
                      <span class:connected={row.health === "connected"} class:inactive={row.health === "inactive"} class="state-label">{connectionLabel(row.health)}</span>
                    </div>
                  {:else}
                    <p class="empty-diagnostic">{lm.no_remote_servers}</p>
                  {/each}
                </div>
              </article>
            </div>

            <article class="card form-card connections-editor">
              {#if (payload.base.servers == null || (payload.base.servers as unknown[]).length === 0)}<p class="hint">{lm.no_servers}</p>{/if}
              <ServersSection bind:payload onChange={markDirty} onError={(m) => setBanner("error", m)} />
            </article>
          </div>
        {:else if active === "deck"}
          <div class="deck-workbench">
            <article class="card deck-workbench-preview"><div class="card-heading"><div><h2>{lm.live_deck}</h2><p>{lm.live_deck_hint}</p></div><span class="badge">{optionLabel(activeValue)}</span></div><div class="deck-surface"><DeckView transport={preview} onJump={jumpToSection} onView={(view) => (deckView = view)} /></div></article>
            <article class="card form-card"><DeckSection bind:payload {editProfile} {reloadRev} onChange={markDirty} onError={(m) => setBanner("error", m)} /></article>
          </div>
        {:else}
          <article class="card form-card">
            {#if active === "view"}
              <ViewSection bind:payload {editProfile} {reloadRev} onChange={markDirty} onError={(m) => setBanner("error", m)} />
            {:else if active === "theme"}
              <ThemeSection bind:payload {editProfile} {reloadRev} onChange={markDirty} onError={(m) => setBanner("error", m)} />
            {:else if active === "macros"}
              <MacrosSection bind:payload {editProfile} onChange={markDirty} onError={(m) => setBanner("error", m)} />
            {:else if active === "start_profiles"}
              <StartProfilesSection bind:payload {editProfile} {reloadRev} onChange={markDirty} onError={(m) => setBanner("error", m)} />
            {:else if active === "notifications"}
              <NotificationsSection bind:payload {editProfile} {reloadRev} onChange={markDirty} onError={(m) => setBanner("error", m)} />
            {:else if active === "safety"}
              <SafetySection bind:payload {editProfile} {reloadRev} onChange={markDirty} onError={(m) => setBanner("error", m)} />
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
          </article>
        {/if}
      {/if}
    </section>
  </div>

  {#if showErrors && errors.length > 0}
    <div class="errlist" role="alert">
      <ul>
        {#each validationIssues as entry}
          <li><button type="button" onclick={() => void focusValidationIssue(entry)}>{entry.message}</button></li>
        {/each}
      </ul>
    </div>
  {/if}

  <footer class="savebar">
    <button onclick={discard} disabled={browserMode || !dirty || busy} title={lm.discard_title}>{lm.discard}</button>
    {#if banner}<Banner kind={banner.kind} message={banner.message} actionLabel={banner.actionLabel} onAction={banner.onAction} />{/if}
    <span class="spacer"></span>
    {#if errors.length > 0}
      <button class="errcount" title={lm.errlist_title} onclick={() => (showErrors = !showErrors)}>
        ⚠ {errorCountLabel(errors.length, locale.lang)} {showErrors ? "▾" : "▸"}
      </button>
    {/if}
    <button onclick={apply} disabled={browserMode || !dirty || busy} title={`${lm.apply_title} (⌘S)`}>{lm.apply}<kbd>⌘S</kbd></button>
  </footer>
</main>

<style>
  :global(*) { box-sizing: border-box; }
  :global(button), :global(select), :global(input) { font: inherit; }
  main {
    display: flex;
    flex-direction: column;
    height: 100dvh;
    min-width: 0;
    background: var(--canvas);
  }
  .topbar { display: flex; align-items: center; gap: 15px; min-height: 52px; padding: 0 16px; border-bottom: 1px solid var(--line); background: #10141a; }
  .brand { display: flex; align-items: center; gap: 9px; margin-right: 7px; letter-spacing: -.015em; }
  .brand strong { font-size: 13px; font-weight: 660; }
  .brand-mark { display: grid; grid-template-columns: repeat(2, 5px); gap: 2px; padding: 5px; border: 1px solid var(--line-strong); border-radius: 6px; background: var(--panel-raised); }
  .brand-mark i { width: 5px; height: 5px; border-radius: 1.5px; background: #849dd3; }
  .brand-mark i:nth-child(2), .brand-mark i:nth-child(3) { background: #6f83ad; }
  .brand-mark i:nth-child(4) { background: #a8b6d4; }
  .status-pill { display: inline-flex; align-items: center; gap: 7px; color: #aeb6c1; font: 10px "SF Mono", ui-monospace, monospace; white-space: nowrap; }
  .status-dot { width: 6px; height: 6px; flex: none; border-radius: 50%; background: var(--st-blocked); }
  .status-dot.ready { background: var(--st-working); }
  .top-spacer, .spacer { flex: 1; }
  .profile-picker { display: flex; align-items: center; gap: 8px; color: var(--text-dim); font-size: 11px; }
  .topbar select { min-width: 138px; height: 30px; padding: 0 28px 0 9px; border: 1px solid var(--line); border-radius: var(--r-control); background: var(--field); color: var(--text); }
  .dirty { color: #e3b56f; font-size: 11px; white-space: nowrap; }
  .dirty.bad { color: #e98990; }
  .body { flex: 1; display: grid; grid-template-columns: 216px minmax(0, 1fr); min-height: 0; }
  .sidebar { display: flex; flex-direction: column; padding: 13px 10px 10px; border-right: 1px solid var(--line); background: var(--sidebar); overflow: auto; }
  .settings-search { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 7px; min-height: 32px; margin-bottom: 14px; padding: 0 8px; border: 1px solid var(--line); border-radius: var(--r-control); background: var(--field); color: var(--text-faint); }
  .settings-search:focus-within { border-color: #607fbf; box-shadow: 0 0 0 2px rgb(111 145 217 / .12); }
  .settings-search input { width: 100%; min-width: 0; padding: 0; border: 0; outline: 0; background: transparent; color: var(--text); font-size: 10px; }
  .settings-search input::placeholder { color: var(--text-faint); }
  .settings-search kbd { padding: 1px 4px; border: 1px solid var(--line); border-radius: 4px; color: var(--text-faint); background: var(--panel); font: 8px "SF Mono", ui-monospace, monospace; }
  .settings-search button { width: 18px; height: 18px; padding: 0; border: 0; border-radius: 4px; background: transparent; color: #8794a5; cursor: pointer; }
  .settings-search button:hover { background: var(--panel-raised); color: var(--text); }
  .nav-empty { margin: 4px 9px; color: var(--text-dim); font-size: 10px; }
  .nav-group { margin-bottom: 12px; }
  .nav-label { display: block; padding: 0 9px 5px; color: #717b89; font-size: 9px; font-weight: 650; letter-spacing: .04em; }
  .sidebar button { display: flex; align-items: center; gap: 9px; width: 100%; min-height: 31px; padding: 0 9px; border: 0; border-radius: 6px; background: none; color: #9da6b2; font-size: 11px; text-align: left; cursor: pointer; }
  .sidebar button:hover { color: #dfe3e8; background: rgb(255 255 255 / .035); }
  .sidebar button.active { color: #edf0f4; background: var(--accent-soft); }
  .sidebar button.problem::after { width: 5px; height: 5px; margin-left: auto; border-radius: 50%; background: var(--st-offline); content: ""; }
  .nav-icon { width: 16px; color: #737e8d; text-align: center; }
  .sidebar button.active .nav-icon { color: #95afe8; }
  .sidebar-version { display: flex; flex-direction: column; gap: 3px; margin-top: auto; padding: 12px 10px 2px; border-top: 1px solid rgb(255 255 255 / .06); color: #657286; font: 9px "SF Mono", ui-monospace, monospace; }
  .sidebar-version strong { color: #8995a5; font-family: inherit; }
  .content { min-width: 0; padding: var(--s6) var(--s8) var(--s10); overflow: auto; }
  .content > * { max-width: var(--measure-form); }
  .content > .overview-stage, .content > .deck-workbench, .content > .connections-workbench {
    max-width: var(--measure-wide);
  }
  .page-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; margin-bottom: 24px; }
  .page-heading h1 { margin: 0; font-size: 23px; font-weight: 680; line-height: 1.2; letter-spacing: -.035em; }
  .title-line { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
  .scope-badge { margin-top: 4px; padding: 2px 6px; border: 1px solid var(--line-strong); border-radius: 5px; color: #abb4c0; background: var(--panel); font: 9px "SF Mono", ui-monospace, monospace; }
  .page-heading p, .card-heading p, .card p { margin: 5px 0 0; color: var(--text-dim); font-size: 11px; }
  .eyebrow, .runtime-label { color: var(--text-dim); font-size: 9px; font-weight: 650; letter-spacing: .04em; }
  .secondary, .icon-button, .savebar button { border: 1px solid var(--line-strong); border-radius: var(--r-control); background: var(--panel-raised); color: #dce1e8; cursor: pointer; }
  .secondary { min-height: 32px; padding: 0 12px; font-size: 11px; font-weight: 630; }
  .secondary:hover, .savebar button:hover:not(:disabled) { background: #222a34; }
  .secondary:active, .icon-button:active, .savebar button:active:not(:disabled) { transform: translateY(1px); }
  .icon-button { width: 31px; height: 31px; padding: 0; }
  .card { border: 1px solid var(--line); border-radius: var(--r-panel); background: var(--panel); }
  .card-heading { display: flex; align-items: center; justify-content: space-between; gap: 14px; }
  .card-heading h2, .runtime-card h2 { margin: 0; font-size: 14px; letter-spacing: -.015em; }
  .overview-stage { display: grid; grid-template-columns: minmax(520px, 1.5fr) minmax(270px, .7fr); gap: var(--s5); align-items: start; margin-top: var(--s5); }
  .live-deck-card { padding: 17px; }
  .live-deck-card .card-heading, .deck-workbench-preview .card-heading { margin-bottom: 16px; }
  .deck-surface { padding: 9px; border: 1px solid #353c46; border-radius: 13px; background: #242a32; box-shadow: inset 0 1px rgb(255 255 255 / .055); }
  .deck-surface :global(.deck) { padding: 0; background: transparent; }
  .deck-surface :global(.grid) { gap: 8px; padding: 4px; background: transparent; }
  .deck-surface :global(.cell), .deck-surface :global(.panel) { border: 1px solid #39414c; border-radius: 8px; box-shadow: 0 2px 5px rgb(0 0 0 / .3); }
  .deck-surface :global(footer.summary) { padding: 8px 4px 1px; color: #9ca8b7; }
  .overview-stack { display: grid; gap: 10px; }
  .badge { display: inline-flex; align-items: center; min-height: 20px; padding: 0 6px; border: 1px solid rgb(85 198 141 / .22); border-radius: 5px; background: transparent; color: #76d7a5; font: 600 9px "SF Mono", ui-monospace, monospace; white-space: nowrap; }
  .badge.warning { border-color: rgb(217 164 93 / .25); background: transparent; color: #e2b674; }
  .connection-row { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 11px; min-height: 51px; border-top: 1px solid var(--line); }
  .connection-row:first-of-type { margin-top: 11px; }
  .connection-row strong, .connection-row small { display: block; }
  .connection-row strong { font-size: 11px; }
  .connection-row small { margin-top: 2px; color: var(--text-dim); font-size: 9px; }
  .deck-workbench { display: grid; grid-template-columns: minmax(480px, 1.15fr) minmax(380px, .85fr); gap: 14px; align-items: start; }
  .deck-workbench-preview { padding: 18px; }
  .connections-workbench { display: grid; gap: 14px; }
  .connection-summary { display: grid; grid-template-columns: minmax(170px, .42fr) 1fr; border-block: 1px solid var(--line); }
  .connection-summary > div { display: grid; grid-template-columns: auto 1fr; align-content: center; column-gap: 12px; min-height: 88px; padding: 14px 18px; border-right: 1px solid var(--line); }
  .connection-summary > div .eyebrow { grid-column: 1 / -1; }
  .connection-summary > div strong { font: 650 27px/1 "SF Mono", ui-monospace, monospace; letter-spacing: -.06em; }
  .connection-summary > div small { align-self: end; padding-bottom: 2px; color: var(--text-dim); font-size: 10px; }
  .connection-summary dl { display: grid; grid-template-columns: repeat(3, 1fr); margin: 0; }
  .connection-summary dl div { display: flex; flex-direction: column; justify-content: center; gap: 5px; min-width: 0; padding: 14px 18px; border-right: 1px solid var(--line); }
  .connection-summary dl div:last-child { border-right: 0; }
  .connection-summary dt { color: var(--text-dim); font-size: 9px; }
  .connection-summary dd { margin: 0; font: 600 12px "SF Mono", ui-monospace, monospace; color: #f1c177; }
  .connection-summary dd.connected { color: var(--st-working); }
  .connection-inventories { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
  .diagnostic-card { overflow: hidden; background: var(--panel); }
  .diagnostic-card > header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 16px 17px; border-bottom: 1px solid var(--line); }
  .diagnostic-card h2 { margin: 0; font-size: 13px; }
  .diagnostic-card header p { max-width: 420px; }
  .diagnostic-card > header > span { min-width: 24px; padding: 3px 6px; border: 1px solid #364252; border-radius: 6px; color: #aeb9c7; font: 600 10px "SF Mono", ui-monospace, monospace; text-align: center; }
  .diagnostic-list { padding: 0 17px; }
  .diagnostic-row { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 11px; min-height: 72px; padding: 11px 0; border-bottom: 1px solid rgb(255 255 255 / .06); }
  .diagnostic-row:last-child { border-bottom: 0; }
  .connection-light { width: 7px; height: 7px; border-radius: 50%; background: var(--st-blocked); }
  .connection-light.connected { background: var(--st-working); }
  .connection-light.unavailable { background: var(--st-offline); }
  .connection-light.inactive { background: #596474; box-shadow: none; }
  .connection-identity { min-width: 0; }
  .connection-identity strong, .connection-identity small { display: block; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .connection-identity strong { font: 600 11px "SF Mono", ui-monospace, monospace; }
  .connection-identity small { margin-top: 3px; color: var(--text-dim); font: 9px "SF Mono", ui-monospace, monospace; }
  .connection-identity small span { margin-right: 7px; color: #69778a; }
  .connection-identity .profile-use { color: #7f8da0; font-family: inherit; }
  .state-label { align-self: start; padding-top: 3px; color: #f1c177; font-size: 9px; white-space: nowrap; }
  .state-label.connected { color: #79e4b5; }
  .state-label.unavailable { color: #ec9299; }
  .state-label.inactive { color: #778395; }
  .empty-diagnostic { min-height: 70px; padding: 18px 0; }
  .connections-editor { margin-top: 2px; }
  .form-card { padding: 0; border: 0; background: transparent; }
  .loading-card { padding: 22px; }
  .hint { color: var(--text-dim); }
  .savebar { display: flex; align-items: center; gap: 10px; min-height: 48px; padding: 7px 14px; border-top: 1px solid var(--line); background: #10141a; }
  .savebar button { min-height: 32px; margin: 0; padding: 0 13px; font-size: 11px; }
  .savebar button kbd { margin-left: 8px; color: rgb(255 255 255 / .72); font: 8px "SF Mono", ui-monospace, monospace; }
  .savebar button:last-child { border-color: #819dd8; background: var(--accent); color: #0d1524; font-weight: 680; }
  .savebar button:disabled { opacity: .42; cursor: default; }
  .errcount { color: #f0838b !important; background: transparent !important; border-color: transparent !important; }
  .errlist { max-height: 120px; padding: 8px 14px; overflow: auto; border-top: 1px solid #4b2529; background: #191012; color: #f08b91; font-size: 12px; }
  .errlist ul { margin: 0; padding-left: 18px; }
  .errlist li { margin: 2px 0; }
  .errlist button { padding: 0; border: 0; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
  .errlist button:hover { text-decoration: underline; }

  @media (max-width: 1120px) {
    .overview-stage, .deck-workbench, .connection-inventories { grid-template-columns: 1fr; }
    .overview-stack { grid-template-columns: repeat(2, 1fr); }
    .connection-card { grid-column: 1 / -1; }
  }
  @media (max-width: 800px) {
    .secondary-status, .topbar > .status-pill { display: none; }
    .body { grid-template-columns: 1fr; }
    .sidebar { flex-direction: row; gap: 5px; padding: 8px; border-right: 0; border-bottom: 1px solid var(--line); overflow-x: auto; overflow-y: hidden; }
    .settings-search { width: 148px; flex: none; margin: 0; }
    .settings-search kbd { display: none; }
    .nav-group { display: flex; flex: none; gap: 5px; margin: 0; }
    .nav-label, .sidebar-version { display: none; }
    .sidebar button { width: auto; min-width: max-content; justify-content: center; }
    .content { padding: 20px 16px 28px; }
    .overview-stack { grid-template-columns: 1fr; }
    .connection-card { grid-column: auto; }
    .connection-summary { grid-template-columns: 1fr; }
    .connection-summary > div { border-right: 0; border-bottom: 1px solid var(--line); }
    .connection-summary dl div { padding-inline: 12px; }
    .form-card { padding: 17px; }
  }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; animation-duration: .01ms !important; }
  }
</style>
