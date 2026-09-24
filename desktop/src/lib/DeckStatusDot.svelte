<script lang="ts">
  // The deck window's only health signal: a small dot in the corner coloured
  // by the worst (not dismissed) problem the runtime's /health shows (plus "a new app version
  // is available" as info). No rows here — the deck window is a glanceable
  // overlay. The tooltip names the top problem; clicking opens Settings →
  // Maintenance in the app window (the `open_maintenance` shell command).
  import type { InvokeFn } from "./deckClient";
  import { bySeverity, healthProblems, worstSeverity, type Severity } from "./healthStatus";
  import { fmt, locale } from "./i18n.svelte";
  import { isDismissed, readDismissals, type Dismissals } from "./noticeDismissals";
  import { NOTICE_MESSAGES, problemText } from "./noticeMessages";
  import { visibilityGatedLoop } from "./pollGate";
  import type { UpdateInfo } from "./updateClient";

  let { fetchHealth = null, intervalMs = 5000, invoke = null, appUpdate = null }: {
    fetchHealth?: (() => Promise<unknown>) | null;
    intervalMs?: number;
    invoke?: InvokeFn | null;
    appUpdate?: UpdateInfo | null;
  } = $props();

  const m = $derived(NOTICE_MESSAGES[locale.lang]);
  let health = $state<unknown>(null);
  let now = $state(Date.now());
  // The app window's "×" (same origin → same localStorage), re-read each poll.
  let dismissals = $state<Dismissals>({});

  const items = $derived.by((): { severity: Severity; text: string }[] => {
    const out = healthProblems(health, now).filter((p) => !isDismissed(dismissals, p)).map((p) => ({ severity: p.severity, text: problemText(p, m, now) }));
    if (appUpdate) out.push({ severity: "info", text: fmt(m.app_update, { version: appUpdate.version }) });
    return bySeverity(out);
  });
  const worst = $derived(worstSeverity(items));
  const title = $derived(
    items.length === 0 ? "" : items.length === 1 ? items[0].text : fmt(m.dot_title_more, { text: items[0].text, n: items.length - 1 }),
  );

  function open(): void {
    void invoke?.("open_maintenance").catch(() => {});
  }

  $effect(() => {
    const fetch = fetchHealth;
    if (!fetch) {
      health = null;
      return;
    }
    const current = visibilityGatedLoop(async () => {
      try {
        const raw = await fetch();
        now = Date.now();
        dismissals = readDismissals();
        health = raw;
      } catch {
        health = null; // DeckView already says offline
      }
    }, () => intervalMs);
    return () => current.stop();
  });
</script>

{#if worst}
  <button
    type="button"
    class="dot {worst}"
    data-severity={worst}
    title={title}
    aria-label={`${m.dot_open}: ${title}`}
    onpointerdown={(e) => e.stopPropagation()}
    onclick={open}
  ></button>
{/if}

<style>
  .dot {
    --tone: var(--sev-info);
    position: absolute;
    top: 7px;
    right: 10px;
    z-index: 5;
    width: 10px;
    height: 10px;
    padding: 0;
    border: 2px solid var(--canvas);
    border-radius: 50%;
    background: var(--tone);
    box-shadow: 0 0 0 1px color-mix(in srgb, var(--tone) 55%, transparent);
    cursor: pointer;
  }
  .dot.error { --tone: var(--sev-error); }
  .dot.warning { --tone: var(--sev-warning); }
  .dot:hover { transform: scale(1.25); }
</style>
