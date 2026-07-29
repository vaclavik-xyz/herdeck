<script lang="ts">
  let { kind = "warning", message, actionLabel, onAction }:
    {
      kind?: "warning" | "error" | "success";
      message: string;
      actionLabel?: string;
      onAction?: () => void;
    } = $props();
</script>

{#if message}
  <div class="banner {kind}" role="status" aria-live="polite">
    <span class="msg">{message}</span>
    {#if actionLabel}
      <button type="button" onclick={() => onAction?.()}>{actionLabel}</button>
    {/if}
  </div>
{/if}

<style>
  .banner { display: flex; align-items: center; gap: 8px; padding: 6px 9px; border: 1px solid currentColor; border-radius: var(--radius-control, 7px); }
  .banner .msg { flex: 1; }
  .warning { background: color-mix(in srgb, var(--st-blocked) 14%, var(--canvas)); color: var(--st-blocked); }
  .error { background: color-mix(in srgb, var(--st-offline) 14%, var(--canvas)); color: var(--st-offline); }
  .success { background: color-mix(in srgb, var(--st-working) 14%, var(--canvas)); color: var(--st-working); }
  .banner button { background: transparent; border: 1px solid currentColor; color: inherit; border-radius: 5px; padding: 2px 8px; cursor: pointer; }
</style>
