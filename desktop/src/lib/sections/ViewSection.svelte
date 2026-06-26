<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import SelectField from "../fields/SelectField.svelte";
  import BooleanField from "../fields/BooleanField.svelte";
  import TriStateListField from "../fields/TriStateListField.svelte";
  import { getAt, setAt, listFieldState, setListField, type ListFieldState, type ConfigPayload } from "../configClient";

  let { payload = $bindable(), onChange }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  const MANAGEMENT = ["launcher_menu", "bottom_row"];

  const management = $derived((getAt(payload, "base", "view", "management") as string) ?? "launcher_menu");
  const agentSlots = $derived((getAt(payload, "base", "view", "agent_slots") as string) ?? "");
  const showProfile = $derived((getAt(payload, "base", "view", "show_profile_on_panel") as boolean) ?? false);

  // Tri-state list fields: absent → backend default, [] → off, non-empty → custom.
  // state/list are derived inline in the template per key (see {#each}).
  const LIST_KEYS = ["bottom_row", "tile_fields", "tile_primary", "tile_secondary"] as const;

  function set(key: string, value: unknown): void {
    payload = setAt(payload, "base", "view", key, value);
    onChange();
  }
  function setTri(key: string, state: ListFieldState, list: string[]): void {
    payload = setListField(payload, "base", "view", key, state, list);
    onChange();
  }
</script>

<h2>View</h2>
<SelectField label="management" value={management} options={MANAGEMENT} onchange={(v) => set("management", v)} />
<TextField label="agent_slots" value={agentSlots} oninput={(v) => set("agent_slots", v)} />
<BooleanField label="show_profile_on_panel" value={showProfile} onchange={(v) => set("show_profile_on_panel", v)} />
{#each LIST_KEYS as key}
  <TriStateListField
    label={key}
    state={listFieldState(payload, "base", "view", key)}
    list={(getAt(payload, "base", "view", key) as string[]) ?? []}
    onchange={(s, l) => setTri(key, s, l)}
  />
{/each}

<style>
  h2 { margin: 0 0 8px; }
</style>
