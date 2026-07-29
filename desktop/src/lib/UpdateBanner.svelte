<script lang="ts">
  // Renders the outcome of an update check, whichever of the two callers in
  // App.svelte drove it (the silent mount check or the tray's manual one) —
  // there is exactly one rendering path so the two can never describe the
  // same state two different ways. An install failure (installError) always
  // wins over the check's own state: it means the user already committed to
  // installing, so it is the more urgent thing to show.
  import Banner from "./Banner.svelte";
  import { defineMessages, fmt, locale } from "./i18n.svelte";
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

  const LM = defineMessages({
    en: {
      available: "Herdeck {version} is available.",
      upToDate: "Herdeck is up to date.",
      checking: "Checking for updates…",
      failed: "Update check failed: {reason}",
      install: "Install and restart",
      installing: "Installing…",
    },
    cs: {
      available: "Je dostupný Herdeck {version}.",
      upToDate: "Herdeck je aktuální.",
      checking: "Kontroluji aktualizace…",
      failed: "Kontrola aktualizací selhala: {reason}",
      install: "Nainstalovat a restartovat",
      installing: "Instaluji…",
    },
  });
  const m = $derived(LM[locale.lang]);
  const installAction = $derived(installing ? m.installing : m.install);
</script>

{#if installError}
  <Banner kind="error" message={installError} />
{:else if state?.kind === "available"}
  <Banner
    kind="warning"
    message={fmt(m.available, { version: state.info.version })}
    actionLabel={installAction}
    onAction={onInstall}
  />
{:else if state?.kind === "up-to-date"}
  <Banner kind="success" message={m.upToDate} />
{:else if state?.kind === "checking"}
  <Banner kind="warning" message={m.checking} />
{:else if state?.kind === "failed"}
  <Banner kind="error" message={fmt(m.failed, { reason: state.reason })} />
{/if}
