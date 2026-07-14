<script lang="ts">
  import TriStateListField from "./TriStateListField.svelte";
  import type { ListFieldState } from "../configClient";

  let { onchange }: { onchange: (state: ListFieldState, list: string[]) => void } = $props();
  let context = $state("profile-a");
  let reloadRev = $state(0);
</script>

<button class="switch" type="button" onclick={() => { context = "profile-b"; }}>switch</button>
<button class="reload" type="button" onclick={() => { reloadRev += 1; }}>reload</button>
<div class="field">
  <TriStateListField
    label="tile_primary"
    state="default"
    list={[]}
    customSeed={[]}
    resetKey={`${context}:${reloadRev}`}
    {onchange}
  />
</div>
