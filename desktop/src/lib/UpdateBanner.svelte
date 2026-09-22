<script lang="ts">
  // Renders the two orthogonal pieces of update state App.svelte tracks
  // (see updateState.ts): a STICKY `availableUpdate` (persists until a later
  // check confirms up-to-date, or an install resolves it away) and a
  // TRANSIENT `notice` (checking/up-to-date/failed, auto-dismissed by
  // App.svelte's own timer). Priority: an install failure (installError)
  // always wins — the user already committed to installing, so it is the
  // more urgent thing to show, and it carries the SAME retry action as
  // "available". Otherwise `notice` renders on top of `availableUpdate`
  // (e.g. "Checking for updates…" briefly covers the install button, and a
  // failure covers it for up to 8s) — the update is still there underneath
  // the whole time, never destroyed, and reappears the moment the notice
  // clears.
  //
  // Renders through exactly ONE `<Banner>` call site (never a per-kind
  // `{:else if}` chain of separate `<Banner>` tags): Banner's own div carries
  // `role="status"`/`aria-live="polite"`, and a live region only reliably
  // announces a CONTENT change on an element that already existed — WKWebView
  // + VoiceOver is unreliable about a region that appears with its message
  // already in it. A single instance persists across every transition
  // instead of being destroyed and recreated at each step.
  import Banner from "./Banner.svelte";
  import { defineMessages, fmt, locale } from "./i18n.svelte";
  import type { Notice } from "./updateState";
  import { releaseNotesUrl, type UpdateInfo } from "./updateClient";

  let {
    availableUpdate = null,
    notice = null,
    installError = "",
    installing = false,
    onInstall,
    onDismissError = undefined,
    onLater = undefined,
  }: {
    availableUpdate?: UpdateInfo | null;
    notice?: Notice | null;
    installError?: string;
    installing?: boolean;
    onInstall: () => void;
    // An install failure stays until the user closes it (it used to vanish
    // after 8s, before anyone had read it).
    onDismissError?: () => void;
    // Hide an available update for the rest of this session (per version).
    onLater?: () => void;
  } = $props();

  const LM = defineMessages({
    en: {
      available: "Herdeck {version} is available.",
      upToDate: "Herdeck is up to date.",
      checking: "Checking for updates…",
      failed: "Update check failed: {reason}",
      install: "Install and restart",
      installing: "Installing…",
      later: "Later",
      dismiss: "Dismiss",
      release_notes: "Release notes",
    },
    cs: {
      available: "Je dostupný Herdeck {version}.",
      upToDate: "Herdeck je aktuální.",
      checking: "Kontroluji aktualizace…",
      failed: "Kontrola aktualizací selhala: {reason}",
      install: "Nainstalovat a restartovat",
      installing: "Instaluji…",
      later: "Později",
      dismiss: "Zavřít",
      release_notes: "Poznámky k vydání",
    },
  });
  const m = $derived(LM[locale.lang]);
  const installAction = $derived(installing ? m.installing : m.install);

  type Presentation = {
    kind: "warning" | "error" | "success";
    message: string;
    actionLabel?: string;
    onAction?: () => void;
    dismissLabel?: string;
    onDismiss?: () => void;
    linkLabel?: string;
    linkHref?: string;
  };

  const view = $derived.by((): Presentation => {
    if (installError) {
      return {
        kind: "error",
        message: installError,
        actionLabel: installAction,
        onAction: onInstall,
        dismissLabel: onDismissError ? m.dismiss : undefined,
        onDismiss: onDismissError,
      };
    }
    switch (notice?.kind) {
      case "checking":
        return { kind: "warning", message: m.checking };
      case "up-to-date":
        return { kind: "success", message: m.upToDate };
      case "failed":
        return { kind: "error", message: fmt(m.failed, { reason: notice.reason }) };
    }
    if (availableUpdate) {
      return {
        kind: "warning",
        message: fmt(m.available, { version: availableUpdate.version }),
        actionLabel: installAction,
        onAction: onInstall,
        dismissLabel: onLater ? m.later : undefined,
        onDismiss: onLater,
        linkLabel: m.release_notes,
        linkHref: releaseNotesUrl(availableUpdate.version),
      };
    }
    return { kind: "warning", message: "" };
  });
</script>

<Banner
  kind={view.kind}
  message={view.message}
  actionLabel={view.actionLabel}
  onAction={view.onAction}
  dismissLabel={view.dismissLabel}
  onDismiss={view.onDismiss}
  linkLabel={view.linkLabel}
  linkHref={view.linkHref}
/>
