<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import SelectField from "../fields/SelectField.svelte";
  import BooleanField from "../fields/BooleanField.svelte";
  import ListField from "../fields/ListField.svelte";
  import { getAt, setAt, putList, type ConfigPayload } from "../configClient";

  let { payload = $bindable(), onChange }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  const MANAGEMENT = ["launcher_menu", "bottom_row"];

  const management = $derived((getAt(payload, "base", "view", "management") as string) ?? "launcher_menu");
  const agentSlots = $derived((getAt(payload, "base", "view", "agent_slots") as string) ?? "");
  const showProfile = $derived((getAt(payload, "base", "view", "show_profile_on_panel") as boolean) ?? false);
  const bottomRow = $derived((getAt(payload, "base", "view", "bottom_row") as string[]) ?? []);
  const tileFields = $derived((getAt(payload, "base", "view", "tile_fields") as string[]) ?? []);
  const tilePrimary = $derived((getAt(payload, "base", "view", "tile_primary") as string[]) ?? []);
  const tileSecondary = $derived((getAt(payload, "base", "view", "tile_secondary") as string[]) ?? []);

  // Scalars use setAt; lists use putList (empty list → omit key → backend default, see Task 4).
  function set(key: string, value: unknown): void {
    payload = setAt(payload, "base", "view", key, value);
    onChange();
  }
  function setList(key: string, list: string[]): void {
    payload = putList(payload, "base", "view", key, list);
    onChange();
  }
</script>

<h2>View</h2>
<SelectField label="management" value={management} options={MANAGEMENT} onchange={(v) => set("management", v)} />
<TextField label="agent_slots" value={agentSlots} oninput={(v) => set("agent_slots", v)} />
<BooleanField label="show_profile_on_panel" value={showProfile} onchange={(v) => set("show_profile_on_panel", v)} />
<ListField label="bottom_row" value={bottomRow} onchange={(v) => setList("bottom_row", v)} />
<ListField label="tile_fields" value={tileFields} onchange={(v) => setList("tile_fields", v)} />
<ListField label="tile_primary" value={tilePrimary} onchange={(v) => setList("tile_primary", v)} />
<ListField label="tile_secondary" value={tileSecondary} onchange={(v) => setList("tile_secondary", v)} />

<style>
  h2 { margin: 0 0 8px; }
</style>
