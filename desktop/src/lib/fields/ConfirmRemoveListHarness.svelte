<script lang="ts">
  import ConfirmRemoveButton from "./ConfirmRemoveButton.svelte";

  let items = $state(["first", "second"]);
  let resetKey = $state(0);

  function remove(index: number): void {
    items = items.filter((_, itemIndex) => itemIndex !== index);
    resetKey += 1;
  }
</script>

{#each items as item, index (index)}
  <div data-item={item}>
    <ConfirmRemoveButton title={`Remove ${item}`} identity={item} {resetKey} onconfirm={() => remove(index)} />
  </div>
{/each}
<output>{items.join(",")}</output>
