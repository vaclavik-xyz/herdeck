import { describe, it, expect, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import DeckView from "./DeckView.svelte";
import type { DeckTransport } from "./deckClient";
import { setLang } from "./i18n.svelte";

// A transport that answers, so the deck comes online and the overlay must go.
function liveTransport(): DeckTransport {
  return {
    fetchState: async () => ({
      version: 1,
      slots: 13,
      has_panel: false,
      panel: 0,
      tiles: {},
      summary: {},
      source: "mock",
      connected: true,
      language: "en",
    }),
    tileImage: async () => null,
    panelImage: async () => null,
    press: async () => ({ ok: true, status: 200, forbidden: false }),
  };
}

function render(props: Record<string, unknown>) {
  const target = document.createElement("div");
  document.body.appendChild(target);
  const instance = mount(DeckView, { target, props });
  flushSync();
  return { target, cleanup: () => { unmount(instance); target.remove(); } };
}

describe("DeckView offline state", () => {
  it("explains the empty grid when there is no transport", () => {
    setLang("en");
    const { target, cleanup } = render({ transport: null });
    try {
      const overlay = target.querySelector(".deck-offline");
      expect(overlay).not.toBeNull();
      expect(overlay?.textContent).toContain("runtime");
    } finally { cleanup(); }
  });

  it("explains it in Czech when the deck speaks Czech", () => {
    setLang("cs");
    const { target, cleanup } = render({ transport: null });
    try {
      expect(target.querySelector(".deck-offline")?.textContent).toContain("runtime");
      expect(target.querySelector(".deck-offline strong")?.textContent).toBe("Čekám na runtime");
    } finally {
      setLang("en");
      cleanup();
    }
  });

  // The compact deck is the floating window, and its footer — the only other
  // surface that says "offline" — is sr-only there (see DeckView.style.test.ts,
  // which owns the CSS half of this). Suppressing the overlay in compact mode
  // left an unreachable runtime looking exactly like a deck with nothing on it:
  // blank keys and no reason why.
  it("explains itself in compact mode too, where the footer is sr-only", () => {
    setLang("en");
    const { target, cleanup } = render({ transport: null, compact: true });
    try {
      const overlay = target.querySelector(".deck-offline");
      expect(overlay, "the compact deck offers no reason for its blank keys").not.toBeNull();
      expect(overlay?.textContent).toContain("Waiting for the runtime");
    } finally { cleanup(); }
  });

  // Sized for a 328px card: the full explanation stays with the desktop window.
  it("shows the pill variant in compact and the full card otherwise", () => {
    setLang("en");
    const small = render({ transport: null, compact: true });
    const large = render({ transport: null });
    try {
      const mini = small.target.querySelector(".deck-offline");
      expect(mini?.classList.contains("mini")).toBe(true);
      expect(mini?.querySelector("p"), "the paragraph does not fit a 328px deck").toBeNull();

      const full = large.target.querySelector(".deck-offline");
      expect(full?.classList.contains("mini")).toBe(false);
      expect(full?.querySelector("p")?.textContent).toContain("as soon as");
    } finally { small.cleanup(); large.cleanup(); }
  });

  // The other direction: an overlay that never clears would sit on top of a
  // working deck, which is worse than the blankness it replaced.
  it("clears once the runtime answers", async () => {
    setLang("en");
    const { target, cleanup } = render({ transport: liveTransport(), compact: true });
    try {
      await vi.waitFor(() => {
        flushSync();
        expect(
          target.querySelector(".deck-offline"),
          "the overlay outlived the connection",
        ).toBeNull();
      });
    } finally { cleanup(); }
  });
});
