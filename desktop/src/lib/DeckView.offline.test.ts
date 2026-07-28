import { describe, it, expect } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import DeckView from "./DeckView.svelte";
import { setLang } from "./i18n.svelte";

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

  it("keeps the compact floating deck free of the overlay", () => {
    setLang("en");
    const { target, cleanup } = render({ transport: null, compact: true });
    try {
      expect(target.querySelector(".deck-offline")).toBeNull();
    } finally { cleanup(); }
  });
});
