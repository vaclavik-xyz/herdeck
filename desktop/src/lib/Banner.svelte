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
  <div class="banner {kind}">
    <span class="msg">{message}</span>
    {#if actionLabel}
      <button type="button" onclick={() => onAction?.()}>{actionLabel}</button>
    {/if}
  </div>
{/if}

<style>
  .banner { display: flex; align-items: center; gap: 8px; padding: 4px 8px; border-radius: 4px; }
  .banner .msg { flex: 1; }
  .warning { background: #2a2410; color: #e0a030; }
  .error { background: #2a1414; color: #e05050; }
  .success { background: #14241a; color: #4caf78; }
  .banner button { background: #1b1b1f; border: 1px solid #2a2a30; color: inherit; border-radius: 4px; padding: 2px 8px; cursor: pointer; }
</style>
