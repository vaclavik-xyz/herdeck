// A request to show one settings section, from outside ConfigApp: the Rust
// `open_maintenance` command's "open-section" event (App.svelte listens) or a
// notice's Details / Fix config… button in the same window. ConfigApp follows `seq`, so asking
// for the same section twice still switches back to it.
export const settingsRequest = $state<{ section: string; seq: number }>({ section: "", seq: 0 });

export function requestSettingsSection(section: string): void {
  settingsRequest.section = section;
  settingsRequest.seq += 1;
}
