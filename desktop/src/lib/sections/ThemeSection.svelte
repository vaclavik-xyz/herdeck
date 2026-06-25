<script lang="ts">
  import TextField from "../fields/TextField.svelte";
  import ListField from "../fields/ListField.svelte";
  import { getAt, setAt, putList, type ConfigPayload } from "../configClient";

  let { payload = $bindable(), onChange }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  // The known status keys (config.py DEFAULT_STATUS_COLORS). Fixed domain → fixed fields.
  const STATUS = ["working", "idle", "blocked", "done", "unknown", "offline"];

  function colorOf(key: string): string {
    return (getAt(payload, "base", "theme", "colors") as Record<string, unknown> | undefined)?.[key] as string ?? "";
  }
  const accents = $derived((getAt(payload, "base", "theme", "server_accents") as string[]) ?? []);

  function setColor(key: string, v: string): void {
    const cur = getAt(payload, "base", "theme", "colors");
    const colors: Record<string, unknown> =
      cur != null && typeof cur === "object" && !Array.isArray(cur) ? { ...(cur as Record<string, unknown>) } : {};
    if (v.trim() === "") delete colors[key];
    else colors[key] = v;
    payload = setAt(payload, "base", "theme", "colors", colors);
    onChange();
  }
  function setAccents(v: string[]): void {
    // list: empty → omit (backend default DEFAULT_SERVER_ACCENTS), not an explicit [].
    payload = putList(payload, "base", "theme", "server_accents", v);
    onChange();
  }
</script>

<h2>Theme</h2>
<fieldset class="colors">
  <legend>colors</legend>
  {#each STATUS as key (key)}
    <TextField label={key} value={colorOf(key)} oninput={(v) => setColor(key, v)} />
  {/each}
</fieldset>
<ListField label="server_accents" value={accents} onchange={setAccents} />

<style>
  h2 { margin: 0 0 8px; }
  .colors { border: 1px solid #2a2a30; border-radius: 6px; margin: 8px 0; padding: 8px 12px; }
  .colors legend { color: #ccc; }
</style>
