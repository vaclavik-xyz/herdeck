// Test-only helper: a deeply reactive props object for `mount()`, so a test
// can change a prop after mount (plain objects passed to mount are not
// reactive, and runes are only allowed in .svelte / .svelte.ts modules).
export function reactiveProps<T extends Record<string, unknown>>(initial: T): T {
  const props = $state(initial);
  return props;
}
