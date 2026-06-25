<script lang="ts">
  import BooleanField from "../fields/BooleanField.svelte";
  import ListField from "../fields/ListField.svelte";
  import { getAt, setAt, putList, type ConfigPayload } from "../configClient";

  let { payload = $bindable(), onChange }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  const approveAlways = $derived((getAt(payload, "base", "safety", "approve_always") as boolean) ?? true);
  const requireConfirmFor = $derived((getAt(payload, "base", "safety", "require_confirm_for") as string[]) ?? []);

  function set(key: string, value: unknown): void {
    payload = setAt(payload, "base", "safety", key, value);
    onChange();
  }
  function setRequireConfirmFor(list: string[]): void {
    payload = putList(payload, "base", "safety", "require_confirm_for", list);
    onChange();
  }
</script>

<h2>Safety</h2>
<BooleanField label="approve_always" value={approveAlways} onchange={(v) => set("approve_always", v)} />
<ListField label="require_confirm_for" value={requireConfirmFor} onchange={setRequireConfirmFor} />

<style>
  h2 { margin: 0 0 8px; }
</style>
