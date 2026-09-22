// The settings window only needs the deck's STATUS (runtime online, agent
// counts, connection health) — never its tile images. DeckView reports a fresh
// view model on every poll step, and the settings window's own status poll
// builds one every second; assigning either unconditionally re-ran every
// derived connection/ribbon computation even when nothing had changed.
import type { DeckViewModel } from "./deckClient";

/** Stable key over exactly the fields the settings window reads. */
export function deckStatusKey(view: DeckViewModel): string {
  return JSON.stringify([
    view.online,
    view.slots,
    view.source,
    view.connected,
    view.summary,
    view.language,
    view.sections,
    view.connections,
    view.localConnections,
  ]);
}

/** `prev` itself when `next` changes nothing the settings window shows, so a
 *  Svelte `$state` assignment of the result is a no-op; otherwise `next`. */
export function adoptDeckStatus(prev: DeckViewModel, next: DeckViewModel): DeckViewModel {
  return deckStatusKey(prev) === deckStatusKey(next) ? prev : next;
}

/** Whether a live DeckView is mounted for this settings page — it then feeds
 *  the status through `onView`, and the settings window's own poll must not
 *  fetch /state a second time. */
export function deckPreviewMounted(active: string, hasPayload: boolean): boolean {
  return active === "overview" || (active === "deck" && hasPayload);
}
