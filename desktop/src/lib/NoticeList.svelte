<script lang="ts">
  // The app window's health notices: one compact row per problem the
  // runtime's /health shows (config that does not load, bridge down / token
  // rejected, version mismatches, D200 problems, unsupported protocol) plus
  // the "new app version" fact. Each row: severity bar + icon, one human
  // sentence (raw facts in title=), ONE primary action where there is one,
  // "Details" (Settings → Maintenance) and × (hidden until the problem's
  // content changes — noticeDismissals.ts). Action results go to the toast
  // stack (toastStore.svelte.ts). Polls /health through the Rust shell (never the
  // token in JS) and renders no rows while everything is fine.
  import Info from "phosphor-svelte/lib/Info";
  import Warning from "phosphor-svelte/lib/Warning";
  import WarningOctagon from "phosphor-svelte/lib/WarningOctagon";
  import X from "phosphor-svelte/lib/X";
  import type { InvokeFn } from "./deckClient";
  import { setHealthProblems } from "./healthState.svelte";
  import {
    bySeverity, configErrorSection, healthProblems,
    type HealthAction, type HealthProblem, type Severity,
  } from "./healthStatus";
  import { fmt, locale } from "./i18n.svelte";
  import { fetchMaintenance, restartDeck, runBridgeUpdate, runtimeService, unitOwner } from "./maintenanceClient";
  import { MAINTENANCE_MESSAGES, bridgeUpdateText, deckOutcomeText } from "./maintenanceMessages";
  import { dismiss, isDismissed, pruneDismissals, readDismissals, writeDismissals, type Dismissals } from "./noticeDismissals";
  import { NOTICE_MESSAGES, actionLabel, problemText } from "./noticeMessages";
  import { visibilityGatedLoop } from "./pollGate";
  import { bridgeUpdateSteps, showToast } from "./toastStore.svelte";
  import { releaseNotesUrl, type UpdateInfo } from "./updateClient";

  let {
    fetchHealth = null,
    intervalMs = 5000,
    invoke = null,
    onOpenSection = null,
    appUpdate = null,
    installing = false,
    onInstall = undefined,
    onLater = undefined,
  }: {
    // `invoke("check_health")`; null (no discovery yet) keeps it silent.
    fetchHealth?: (() => Promise<unknown>) | null;
    intervalMs?: number;
    // Tauri `invoke` for the actions; null = no action buttons.
    invoke?: InvokeFn | null;
    // Switch the settings (this window) to a section; null = ask the shell
    // (`open_maintenance`).
    onOpenSection?: ((section: string) => void) | null;
    // A newer app release (updateState.ts), shown as an info row.
    appUpdate?: UpdateInfo | null;
    installing?: boolean;
    onInstall?: () => void;
    onLater?: () => void;
  } = $props();

  const m = $derived(NOTICE_MESSAGES[locale.lang]);
  const mm = $derived(MAINTENANCE_MESSAGES[locale.lang]);

  let health = $state<unknown>(null);
  let now = $state(Date.now());
  let dismissals = $state<Dismissals>(readDismissals());
  // "Restart runtime" only when the runtime is this app's own service (the
  // Rust runtime_service guard refuses anything else).
  let runtimeRestartable = $state(false);
  let busy = $state<Record<string, boolean>>({});
  let loop: ReturnType<typeof visibilityGatedLoop> | null = null;

  const problems = $derived(healthProblems(health, now));

  interface Row {
    key: string;
    severity: Severity;
    text: string;
    detail: string;
    action: HealthAction | null;
    problem: HealthProblem | null;
  }

  const rows = $derived.by((): Row[] => {
    const out: Row[] = problems
      .filter((p) => !isDismissed(dismissals, p))
      .map((p) => ({
        key: p.key,
        severity: p.severity,
        text: problemText(p, m, now),
        detail: p.detail,
        action: p.action?.kind === "restart_runtime" && !runtimeRestartable ? null : p.action,
        problem: p,
      }));
    if (appUpdate) {
      out.push({
        key: "app_update",
        severity: "info",
        text: fmt(m.app_update, { version: appUpdate.version }),
        detail: `${appUpdate.current_version} → ${appUpdate.version}`,
        action: null,
        problem: null,
      });
    }
    return bySeverity(out);
  });

  function actionKey(a: HealthAction): string {
    return a.kind === "update_bridge" ? `update:${a.serverId}` : a.kind;
  }

  function severityLabel(s: Severity): string {
    return s === "error" ? m.sev_error : s === "warning" ? m.sev_warning : m.sev_info;
  }

  function openSection(section: string): void {
    if (onOpenSection) onOpenSection(section);
    else void invoke?.("open_maintenance").catch(() => {});
  }

  function hide(row: Row): void {
    if (!row.problem) {
      onLater?.();
      return;
    }
    dismissals = dismiss(dismissals, row.problem);
    writeDismissals(dismissals);
  }

  async function run(row: Row): Promise<void> {
    const a = row.action;
    const call = invoke;
    if (!a || !call) return;
    if (a.kind === "fix_config") {
      openSection(configErrorSection(row.detail));
      return;
    }
    const key = actionKey(a);
    if (busy[key]) return;
    busy[key] = true;
    try {
      if (a.kind === "update_bridge") await updateBridge(call, a.serverId);
      else if (a.kind === "restart_deck") await restartTheDeck(call);
      else await restartRuntime(call);
    } finally {
      busy[key] = false;
      loop?.kick();
    }
  }

  async function updateBridge(call: InvokeFn, id: string): Promise<void> {
    const toastId = `bridge-update:${id}`;
    const labels = { download: m.step_download, install: m.step_install, verify: m.step_verify, restart: m.step_restart };
    const progress = (stages: string[], code: string) => bridgeUpdateSteps(stages, code, labels);
    showToast({ id: toastId, kind: "progress", text: fmt(m.bridge_updating, { id }), steps: progress([], "pending") });
    const view = await runBridgeUpdate(call, id, (v) => {
      if (v.code === "pending") {
        showToast({
          id: toastId, kind: "progress", text: fmt(m.bridge_updating, { id }),
          steps: progress(v.progress.map((p) => p.stage), v.code),
          detail: v.progress.at(-1)?.message,
        });
      }
    });
    const steps = progress(view.progress.map((p) => p.stage), view.code);
    if (view.code === "updated") {
      const version = view.target ?? String((health as Record<string, unknown> | null)?.version ?? "?");
      showToast({ id: toastId, kind: "success", text: fmt(m.bridge_updated, { id, version }), steps, detail: view.message });
      return;
    }
    const t = bridgeUpdateText(view, mm);
    showToast({
      id: toastId,
      kind: view.ok ? "info" : "error",
      text: t.text,
      steps: view.progress.length > 0 ? steps : undefined,
      command: t.command ?? undefined,
      detail: [view.code, view.message, view.output].filter(Boolean).join("\n"),
    });
  }

  async function restartTheDeck(call: InvokeFn): Promise<void> {
    const toastId = "restart-deck";
    showToast({ id: toastId, kind: "progress", text: m.deck_restarting });
    const outcome = await restartDeck(call);
    const o = deckOutcomeText(outcome, mm);
    showToast({
      id: toastId,
      kind: o.ok ? "success" : "error",
      text: o.text,
      command: o.command ?? undefined,
      detail: [outcome.outcome, outcome.error].filter(Boolean).join(": "),
    });
  }

  async function restartRuntime(call: InvokeFn): Promise<void> {
    const toastId = "restart-runtime";
    showToast({ id: toastId, kind: "progress", text: m.runtime_restarting });
    const r = await runtimeService(call, "restart");
    showToast({
      id: toastId,
      kind: r.ok ? "success" : "error",
      text: r.ok ? m.runtime_restarted : m.runtime_restart_failed,
      detail: r.ok ? undefined : r.detail || (r.timedOut ? "timeout" : `exit ${r.exitCode ?? "?"}`),
    });
  }

  // Poll /health; a successful read also prunes dismissals of problems that
  // are gone and publishes the problems for the Maintenance badge.
  $effect(() => {
    const fetch = fetchHealth;
    if (!fetch) {
      health = null;
      setHealthProblems([]);
      return;
    }
    const current = visibilityGatedLoop(async () => {
      try {
        const raw = await fetch();
        now = Date.now();
        health = raw;
        const found = healthProblems(raw, now);
        setHealthProblems(found);
        const pruned = pruneDismissals(dismissals, found.map((p) => p.key));
        if (pruned !== dismissals) {
          dismissals = pruned;
          writeDismissals(pruned);
        }
      } catch {
        health = null; // runtime not reachable: DeckView already says offline
        setHealthProblems([]);
      }
    }, () => intervalMs);
    loop = current;
    return () => { current.stop(); if (loop === current) loop = null; };
  });

  // Ask GET /maintenance who owns the runtime only while a runtime version
  // mismatch is showing (the one problem "Restart runtime" fixes).
  const mismatch = $derived(problems.some((p) => p.kind === "runtime_mismatch"));
  $effect(() => {
    const call = invoke;
    if (!mismatch || !call) {
      runtimeRestartable = false;
      return;
    }
    let alive = true;
    void fetchMaintenance(call).then((r) => {
      if (alive) runtimeRestartable = r.kind === "ok" && unitOwner(r.status) === "this_app";
    });
    return () => { alive = false; };
  });
</script>

<section class="notices" aria-label={m.notices_label} role="status" aria-live="polite">
  {#each rows as row (row.key)}
    <div class="notice {row.severity}" data-notice={row.key} data-severity={row.severity}>
      <span class="icon" aria-hidden="true">
        {#if row.severity === "error"}<WarningOctagon size={15} weight="fill" />
        {:else if row.severity === "warning"}<Warning size={15} weight="fill" />
        {:else}<Info size={15} weight="fill" />{/if}
      </span>
      <span class="sr-only">{severityLabel(row.severity)}:</span>
      <p class="text" title={row.detail || undefined}>{row.text}</p>
      <div class="actions">
        {#if row.key === "app_update" && onInstall}
          <button type="button" class="primary" data-action="install_update" disabled={installing} onclick={() => onInstall?.()}>
            {installing ? m.installing : m.install_update}
          </button>
        {:else if row.action && invoke}
          {@const key = actionKey(row.action)}
          <button type="button" class="primary" data-action={key} disabled={busy[key] === true} onclick={() => run(row)}>
            {busy[key] ? m.working : actionLabel(row.action, m)}
          </button>
        {/if}
        {#if row.key === "app_update" && appUpdate}
          <a class="link" href={releaseNotesUrl(appUpdate.version)} target="_blank" rel="noopener noreferrer">{m.release_notes}</a>
        {:else}
          <button type="button" class="link" data-details title={m.details_title} onclick={() => openSection("maintenance")}>{m.details}</button>
        {/if}
        {#if row.key !== "app_update" || onLater}
          {@const hideLabel = row.key === "app_update" ? m.later : m.dismiss}
          <button type="button" class="dismiss" data-dismiss title={hideLabel} aria-label={hideLabel} onclick={() => hide(row)}>
            <X size={12} weight="bold" aria-hidden="true" />
          </button>
        {/if}
      </div>
    </div>
  {/each}
</section>

<style>
  .notices { display: flex; flex-direction: column; gap: var(--s1); }
  .notice {
    --tone: var(--sev-info);
    position: relative;
    display: flex;
    align-items: center;
    gap: var(--s2);
    min-height: 36px;
    padding: var(--s1) var(--s1) var(--s1) var(--s3);
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: var(--r-control);
    background: color-mix(in srgb, var(--tone) 7%, var(--panel-raised));
    box-shadow: 0 6px 18px color-mix(in srgb, var(--canvas) 55%, transparent);
    color: var(--text);
    font: var(--t-help);
  }
  .notice::before { content: ""; position: absolute; inset: 0 auto 0 0; width: 3px; background: var(--tone); }
  .notice.error { --tone: var(--sev-error); }
  .notice.warning { --tone: var(--sev-warning); }
  .icon { display: grid; flex: none; place-items: center; color: var(--tone); }
  .text { flex: 1; min-width: 0; margin: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .actions { display: flex; flex: none; align-items: center; gap: 2px; }
  button { font: inherit; cursor: pointer; }
  .primary {
    height: 26px;
    padding: 0 var(--s3);
    border: 1px solid color-mix(in srgb, var(--tone) 55%, var(--line-strong));
    border-radius: 6px;
    background: color-mix(in srgb, var(--tone) 16%, var(--field));
    color: var(--text);
    font-weight: 600;
  }
  .primary:hover:not(:disabled) { background: color-mix(in srgb, var(--tone) 26%, var(--field)); }
  .link {
    height: 26px;
    padding: 0 var(--s2);
    border: 0;
    background: transparent;
    color: var(--text-dim);
    line-height: 26px;
    text-decoration: none;
  }
  .link:hover { color: var(--text); text-decoration: underline; text-underline-offset: 2px; }
  .dismiss {
    display: grid;
    place-items: center;
    width: 24px;
    height: 24px;
    padding: 0;
    border: 0;
    border-radius: 5px;
    background: transparent;
    color: var(--text-faint);
  }
  .dismiss:hover { background: var(--key); color: var(--text); }
  .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
</style>
