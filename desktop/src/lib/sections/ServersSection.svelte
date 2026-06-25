<script lang="ts">
  import type { ConfigPayload } from "../configClient";
  let { payload, onChange, onError }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();
</script>

<p class="hint">Servers — řez 3 Task 6.</p>
