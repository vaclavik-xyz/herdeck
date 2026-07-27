<script lang="ts">
  import { onDestroy } from "svelte";
  import { t } from "../i18n.svelte";

  let { title, onconfirm, identity = "", resetKey = 0 }:
    { title: string; onconfirm: () => void; identity?: string; resetKey?: string | number } = $props();

  let armed = $state(false);
  let resetTimer: ReturnType<typeof setTimeout> | undefined;
  let previousIdentity: string | undefined;
  let previousResetKey: string | number | undefined;
  let resetSeen = false;

  $effect(() => {
    if (resetSeen && (identity !== previousIdentity || resetKey !== previousResetKey)) {
      if (resetTimer) clearTimeout(resetTimer);
      armed = false;
    }
    previousIdentity = identity;
    previousResetKey = resetKey;
    resetSeen = true;
  });

  function press(): void {
    if (armed) {
      if (resetTimer) clearTimeout(resetTimer);
      armed = false;
      onconfirm();
      return;
    }
    armed = true;
    resetTimer = setTimeout(() => (armed = false), 4000);
  }

  onDestroy(() => {
    if (resetTimer) clearTimeout(resetTimer);
  });
</script>

<button
  type="button"
  class="confirm-remove"
  class:armed
  {title}
  aria-label={armed ? t("widget.confirm_remove") : title}
  onclick={press}
>{armed ? t("widget.confirm_remove") : "×"}</button>

<style>
  button { color: var(--st-offline) !important; }
  button.armed { border-color: var(--st-offline) !important; background: var(--panel-raised) !important; color: var(--text) !important; }
</style>
