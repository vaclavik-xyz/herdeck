<script lang="ts">
  // Renders the outcome of an update check, whichever of the two callers in
  // App.svelte drove it (the silent mount check or the tray's manual one) —
  // there is exactly one rendering path so the two can never describe the
  // same state two different ways. An install failure (installError) always
  // wins over the check's own state: it means the user already committed to
  // installing, so it is the more urgent thing to show — and it carries the
  // SAME retry action as "available", not a dead end: installUpdate clears
  // updateError back to "" the moment a retry starts, so nothing else needs
  // to sweep it away.
  //
  // Renders through exactly ONE `<Banner>` call site (never a per-kind
  // `{:else if}` chain of separate `<Banner>` tags): Banner's own div carries
  // `role="status"`/`aria-live="polite"`, and a live region only reliably
  // announces a CONTENT change on an element that already existed — WKWebView
  // + VoiceOver is unreliable about a region that appears with its message
  // already in it. A single instance persists across checking -> available ->
  // up-to-date/failed instead of being destroyed and recreated at each step.
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

  type Presentation = {
    kind: "warning" | "error" | "success";
    message: string;
    actionLabel?: string;
    onAction?: () => void;
  };

  const view = $derived.by((): Presentation => {
    if (installError) {
      return { kind: "error", message: installError, actionLabel: installAction, onAction: onInstall };
    }
    switch (state?.kind) {
      case "available":
        return {
          kind: "warning",
          message: fmt(m.available, { version: state.info.version }),
          actionLabel: installAction,
          onAction: onInstall,
        };
      case "up-to-date":
        return { kind: "success", message: m.upToDate };
      case "checking":
        return { kind: "warning", message: m.checking };
      case "failed":
        return { kind: "error", message: fmt(m.failed, { reason: state.reason }) };
      default:
        return { kind: "warning", message: "" };
    }
  });
</script>

<Banner kind={view.kind} message={view.message} actionLabel={view.actionLabel} onAction={view.onAction} />
