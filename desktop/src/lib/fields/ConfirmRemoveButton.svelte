<script lang="ts">
  import { onDestroy } from "svelte";
  import { t } from "../i18n.svelte";

  let { title, onconfirm }: { title: string; onconfirm: () => void } = $props();

  let armed = $state(false);
  let resetTimer: ReturnType<typeof setTimeout> | undefined;

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
  button { color: #e8878e !important; }
  button.armed { border-color: #8d3e45 !important; background: #351b1f !important; color: #ffb1b6 !important; }
</style>
