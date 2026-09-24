<script lang="ts">
  // The app window's toast stack (bottom-right). Always mounted, so its
  // aria-live region exists before the first toast (see Banner.svelte for why
  // a region that appears with its first message is not announced reliably).
  import { locale } from "./i18n.svelte";
  import { NOTICE_MESSAGES } from "./noticeMessages";
  import Toast from "./Toast.svelte";
  import { closeToast, toasts, type Toast as ToastData } from "./toastStore.svelte";

  let { onClose = undefined }: {
    // Called after a toast was closed by its × (App clears the update state
    // a toast mirrors).
    onClose?: (toast: ToastData) => void;
  } = $props();

  const m = $derived(NOTICE_MESSAGES[locale.lang]);

  function close(t: ToastData): void {
    closeToast(t.id);
    onClose?.(t);
  }
</script>

<section class="toasts" aria-label={m.toasts_label} aria-live="polite">
  {#each toasts.items as toast (toast.id)}
    <Toast {toast} onClose={() => close(toast)} />
  {/each}
</section>

<style>
  .toasts {
    position: fixed;
    right: var(--s4);
    bottom: var(--s4);
    z-index: 40;
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: var(--s2);
    pointer-events: none;
  }
</style>
