<script lang="ts">
  // One toast: severity bar + icon, a sentence, an optional stepper for a
  // long action (bridge update) and an optional copyable command. The ×
  // closes it; success toasts also leave on their own (toastStore.svelte.ts).
  import CheckCircle from "phosphor-svelte/lib/CheckCircle";
  import Info from "phosphor-svelte/lib/Info";
  import WarningCircle from "phosphor-svelte/lib/WarningCircle";
  import X from "phosphor-svelte/lib/X";
  import { locale } from "./i18n.svelte";
  import { NOTICE_MESSAGES } from "./noticeMessages";
  import type { Toast } from "./toastStore.svelte";

  let { toast, onClose }: { toast: Toast; onClose: () => void } = $props();

  const m = $derived(NOTICE_MESSAGES[locale.lang]);
  let copied = $state(false);

  async function copy(command: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(command);
      copied = true;
    } catch {
      copied = false;
    }
  }
</script>

<div class="toast {toast.kind}" data-toast={toast.id} data-kind={toast.kind} role={toast.kind === "error" ? "alert" : undefined}>
  <span class="icon" aria-hidden="true">
    {#if toast.kind === "success"}<CheckCircle size={16} weight="fill" />
    {:else if toast.kind === "error"}<WarningCircle size={16} weight="fill" />
    {:else if toast.kind === "progress"}<span class="spinner"></span>
    {:else}<Info size={16} weight="fill" />{/if}
  </span>
  <div class="body">
    <p class="text" title={toast.detail || undefined}>{toast.text}</p>
    {#if toast.steps && toast.steps.length > 0}
      <ol class="steps">
        {#each toast.steps as step (step.key)}
          <li class={step.state} data-step={step.key} data-state={step.state}>
            <span class="mark" aria-hidden="true"></span>{step.label}
          </li>
        {/each}
      </ol>
    {/if}
    {#if toast.command}
      <div class="command">
        <code>{toast.command}</code>
        <button type="button" class="copy" title={m.copy_title} aria-label={m.copy_title} onclick={() => copy(toast.command ?? "")}>{copied ? "✓" : "⧉"}</button>
      </div>
    {/if}
  </div>
  <button type="button" class="close" title={m.close} aria-label={m.close} onclick={onClose}>
    <X size={12} weight="bold" aria-hidden="true" />
  </button>
</div>

<style>
  .toast {
    --tone: var(--sev-info);
    --tone-text: var(--sev-info-text);
    position: relative;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: start;
    gap: var(--s2);
    width: min(360px, calc(100vw - 32px));
    padding: var(--s2) var(--s2) var(--s2) var(--s3);
    overflow: hidden;
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--panel-raised);
    box-shadow: 0 10px 30px color-mix(in srgb, var(--canvas) 80%, transparent);
    color: var(--text);
    font: var(--t-help);
    pointer-events: auto;
  }
  .toast::before { content: ""; position: absolute; inset: 0 auto 0 0; width: 3px; background: var(--tone); }
  .toast.success { --tone: var(--sev-success); --tone-text: var(--sev-success-text); }
  .toast.error { --tone: var(--sev-error); --tone-text: var(--sev-error-text); }
  .icon { display: grid; place-items: center; width: 16px; height: 18px; color: var(--tone); }
  .text { margin: 0; line-height: 18px; overflow-wrap: anywhere; }
  .error .text { color: var(--tone-text); }
  .spinner {
    width: 11px;
    height: 11px;
    border: 2px solid color-mix(in srgb, var(--tone) 35%, transparent);
    border-top-color: var(--tone);
    border-radius: 50%;
    animation: spin .8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .steps { display: flex; flex-wrap: wrap; gap: var(--s1) var(--s3); margin: var(--s2) 0 0; padding: 0; list-style: none; color: var(--text-faint); }
  .steps li { display: flex; align-items: center; gap: 5px; }
  .steps .mark { width: 7px; height: 7px; border: 1.5px solid currentColor; border-radius: 50%; }
  .steps li.done { color: var(--text-dim); }
  .steps li.done .mark { border-color: var(--sev-success); background: var(--sev-success); }
  .steps li.active { color: var(--text); }
  .steps li.active .mark { border-color: var(--sev-info); background: color-mix(in srgb, var(--sev-info) 45%, transparent); }
  .steps li.failed { color: var(--sev-error-text); }
  .steps li.failed .mark { border-color: var(--sev-error); background: var(--sev-error); }
  .command { display: flex; gap: var(--s1); margin-top: var(--s2); }
  .command code { flex: 1; min-width: 0; overflow-x: auto; padding: 3px 6px; border-radius: 5px; background: var(--field); font: var(--t-mono); white-space: nowrap; }
  button { border: 0; background: transparent; color: var(--text-faint); cursor: pointer; }
  button:hover { color: var(--text); }
  .close { display: grid; place-items: center; width: 20px; height: 20px; padding: 0; border-radius: 5px; }
  .close:hover, .copy:hover { background: var(--key); }
  .copy { padding: 0 6px; border-radius: 5px; }
</style>
