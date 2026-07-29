<script lang="ts">
  // The role="status"/aria-live region lives on THIS wrapper, unconditionally
  // rendered whenever Banner itself is mounted — not on the inner `.banner`
  // box, which still only appears with a message. A live region only
  // reliably announces a CONTENT change on an element that already existed;
  // WKWebView + VoiceOver (the actual macOS target) is unreliable about a
  // region that appears with its first message already in it. Callers that
  // mount Banner unconditionally (see UpdateBanner) get a real persistent
  // region this way; callers that mount it only once there is something to
  // say (ConfigApp) are unaffected either way, since their message is never
  // empty for as long as Banner exists.
  let { kind = "warning", message, actionLabel, onAction }:
    {
      kind?: "warning" | "error" | "success";
      message: string;
      actionLabel?: string;
      onAction?: () => void;
    } = $props();
</script>

<div role="status" aria-live="polite">
  {#if message}
    <div class="banner {kind}">
      <span class="msg">{message}</span>
      {#if actionLabel}
        <button type="button" onclick={() => onAction?.()}>{actionLabel}</button>
      {/if}
    </div>
  {/if}
</div>

<style>
  .banner { display: flex; align-items: center; gap: 8px; padding: 6px 9px; border: 1px solid currentColor; border-radius: var(--radius-control, 7px); }
  .banner .msg { flex: 1; }
  .warning { background: color-mix(in srgb, var(--st-blocked) 14%, var(--canvas)); color: var(--st-blocked); }
  .error { background: color-mix(in srgb, var(--st-offline) 14%, var(--canvas)); color: var(--st-offline); }
  .success { background: color-mix(in srgb, var(--st-working) 14%, var(--canvas)); color: var(--st-working); }
  .banner button { background: transparent; border: 1px solid currentColor; color: inherit; border-radius: 5px; padding: 2px 8px; cursor: pointer; }
</style>
