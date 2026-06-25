import { describe, it, expect } from "vitest";
import NumberField from "./NumberField.svelte";
import BooleanField from "./BooleanField.svelte";
import SelectField from "./SelectField.svelte";

// Compile-smoke only: importing a .svelte compiles it (catches syntax/compile errors)
// without a render/interaction harness. New widgets are added here as they are created.
describe("field widget compile-smoke", () => {
  it("compiles the scalar widgets", () => {
    expect(NumberField).toBeTruthy();
    expect(BooleanField).toBeTruthy();
    expect(SelectField).toBeTruthy();
  });
});
