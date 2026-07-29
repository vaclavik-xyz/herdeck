<script lang="ts">
  // Renders the outcome of an update check, whichever of the two callers in
  // App.svelte drove it (the silent mount check or the tray's manual one) —
  // there is exactly one rendering path so the two can never describe the
  // same state two different ways. An install failure (installError) always
  // wins over the check's own state: it means the user already committed to
  // installing, so it is the more urgent thing to show.
  import Banner from "./Banner.svelte";
  import { locale } from "./i18n.svelte";
  import type { UpdateCheckState } from "./updateClient";

  let {
    state = null,
    installError = "",
    installing = false,
    onInstall,
  }: {
    state?: UpdateCheckState | null;
    installError?: string;
    installing?: boolean;
    onInstall: () => void;
  } = $props();

  const installAction = $derived(
    locale.lang === "cs"
      ? installing ? "Instaluji…" : "Nainstalovat a restartovat"
      : installing ? "Installing…" : "Install and restart",
  );
</script>

{#if installError}
  <Banner kind="error" message={installError} />
{:else if state?.kind === "available"}
  <Banner
    kind="warning"
    message={locale.lang === "cs"
      ? `Je dostupný Herdeck ${state.info.version}.`
      : `Herdeck ${state.info.version} is available.`}
    actionLabel={installAction}
    onAction={onInstall}
  />
{:else if state?.kind === "up-to-date"}
  <Banner
    kind="success"
    message={locale.lang === "cs" ? "Herdeck je aktuální." : "Herdeck is up to date."}
  />
{:else if state?.kind === "checking"}
  <Banner
    kind="warning"
    message={locale.lang === "cs" ? "Kontroluji aktualizace…" : "Checking for updates…"}
  />
{:else if state?.kind === "failed"}
  <Banner
    kind="error"
    message={locale.lang === "cs"
      ? `Kontrola aktualizací selhala: ${state.reason}`
      : `Update check failed: ${state.reason}`}
  />
{/if}
