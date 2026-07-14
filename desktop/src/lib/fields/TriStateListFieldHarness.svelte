<script lang="ts">
  import TriStateListField from "./TriStateListField.svelte";
  import type { ListFieldState } from "../configClient";

  let { onchange }: { onchange: (state: ListFieldState, list: string[]) => void } = $props();
  let context = $state("profile-a");
</script>

<button class="switch" type="button" onclick={() => { context = "profile-b"; }}>switch</button>
<div class="field">
  <TriStateListField
    label="tile_primary"
    state="default"
    list={[]}
    customSeed={[]}
    resetKey={context}
    {onchange}
  />
</div>
