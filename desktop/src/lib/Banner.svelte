<script lang="ts">
  // The role="status"/aria-live region lives on THIS wrapper, unconditionally
  // rendered whenever Banner itself is mounted — not on the inner `.banner`
  // box, which still only appears with a message. A live region only
  // reliably announces a CONTENT change on an element that already existed;
  // WKWebView + VoiceOver (the actual macOS target) is unreliable about a
  // region that appears with its first message already in it. Callers that
  // mount Banner unconditionally (see Toasts/NoticeList) get a real persistent
  // region this way; callers that mount it only once there is something to
  // say (ConfigApp) are unaffected either way, since their message is never
  // empty for as long as Banner exists.
  let { kind = "warning", message, actionLabel, onAction, dismissLabel, onDismiss, linkLabel, linkHref }:
    {
      kind?: "warning" | "error" | "success";
      message: string;
      actionLabel?: string;
      onAction?: () => void;
      // A second, quieter button that closes the banner ("Later", "Dismiss").
      dismissLabel?: string;
      onDismiss?: () => void;
      // An external link (e.g. release notes), opened outside the app.
      linkLabel?: string;
      linkHref?: string;
    } = $props();
</script>

<div role="status" aria-live="polite">
  {#if message}
    <div class="banner {kind}">
      <span class="msg">{message}</span>
      {#if linkLabel && linkHref}
        <a href={linkHref} target="_blank" rel="noopener noreferrer">{linkLabel}</a>
      {/if}
      {#if actionLabel}
        <button type="button" onclick={() => onAction?.()}>{actionLabel}</button>
      {/if}
      {#if dismissLabel}
        <button type="button" class="dismiss" title={dismissLabel} onclick={() => onDismiss?.()}>{dismissLabel}</button>
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
  .banner { flex-wrap: wrap; }
  .banner button { background: transparent; border: 1px solid currentColor; color: inherit; border-radius: 5px; padding: 2px 8px; cursor: pointer; }
  .banner button.dismiss { border-color: transparent; opacity: .85; }
  .banner a { color: inherit; text-decoration: underline; text-underline-offset: 2px; }
</style>
