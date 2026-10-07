import { afterEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import DockCard from "./DockCard.svelte";
import { setLang } from "./i18n.svelte";
import { agentDetail, dockAgent, fakeAgentTransport, DOCK_PROMPT } from "./dockFixtures";
import type { DockAgent } from "./dockClient";
import type { AgentTransport } from "./agentCardClient";
import { flushState } from "./dockCardHarness.svelte";

function render(props: { agent?: DockAgent | null; idle?: DockAgent[]; transport: AgentTransport }) {
  const onClose = vi.fn();
  const onSent = vi.fn();
  const target = document.createElement("div");
  document.body.appendChild(target);
  const instance = mount(DockCard, {
    target,
    props: { sinceOf: (a: DockAgent) => a.sinceS, iconUrl: () => null, onClose, onSent, ...props },
  });
  flushSync();
  return { target, onClose, onSent, cleanup: () => { unmount(instance); target.remove(); } };
}

const buttons = (t: HTMLElement) => Array.from(t.querySelectorAll<HTMLButtonElement>("button.opt"));

afterEach(() => setLang("en"));

describe("DockCard (agent)", () => {
  it("reads a blocked prompt fresh and offers one coloured button per option", async () => {
    const transport = fakeAgentTransport();
    const { target, cleanup } = render({ agent: dockAgent(), transport });
    try {
      await vi.waitFor(() => expect(buttons(target)).toHaveLength(3));
      expect(transport.detailCalls[0]).toEqual({ target: { serverId: "prod", paneId: "p0" }, refresh: true });
      expect(target.querySelector(".prompt")?.textContent).toBe(DOCK_PROMPT);
      expect(buttons(target).map((b) => b.className.match(/tone-\S+/)?.[0])).toEqual([
        "tone-approve",
        "tone-approve_always",
        "tone-deny",
      ]);
      expect(buttons(target)[0].textContent?.trim()).toBe("1. Yes");
      expect(target.querySelector(".title")?.textContent).toBe("Bank API");
      expect(target.querySelector(".meta")?.textContent).toBe("macdoktor-crm · feat/bank");
      expect(target.textContent).toContain("Needs you");
      expect(target.textContent).toContain("for 4m");
    } finally {
      cleanup();
    }
  });

  it("answers with the option key and the prompt revision it was read under", async () => {
    const transport = fakeAgentTransport();
    const { target, onSent, cleanup } = render({ agent: dockAgent(), transport });
    try {
      await vi.waitFor(() => expect(buttons(target)).toHaveLength(3));
      buttons(target)[0].click();
      await vi.waitFor(() => expect(target.textContent).toContain("Sent ✓"));
      expect(transport.actCalls).toEqual([
        { action: "answer", ref: { serverId: "prod", paneId: "p0" }, extra: { key: "1", revision: "rev-1" } },
      ]);
      expect(onSent).toHaveBeenCalledTimes(1);
      expect(buttons(target)).toHaveLength(0);
    } finally {
      cleanup();
    }
  });

  it("a confirm option arms before it answers", async () => {
    const detail = agentDetail({
      options: [{ key: "3", label: "No", id: "deny", kind: "option", confirm: true }],
    });
    const transport = fakeAgentTransport({ details: [{ kind: "ok", detail }] });
    const { target, cleanup } = render({ agent: dockAgent(), transport });
    try {
      await vi.waitFor(() => expect(buttons(target)).toHaveLength(1));
      buttons(target)[0].click();
      flushSync();
      expect(transport.actCalls).toHaveLength(0);
      expect(buttons(target)[0].textContent?.trim()).toBe("Sure? Click again");
      buttons(target)[0].click();
      await vi.waitFor(() => expect(transport.actCalls).toHaveLength(1));
      expect(transport.actCalls[0].extra).toEqual({ key: "3", revision: "rev-1" });
    } finally {
      cleanup();
    }
  });

  it("a stale answer refetches and shows the new prompt without resending", async () => {
    const transport = fakeAgentTransport({
      details: [
        { kind: "ok", detail: agentDetail() },
        { kind: "ok", detail: agentDetail({ prompt: "Edit file?\n❯ 1. Yes\n  2. No", revision: "rev-2" }) },
      ],
      outcomes: [{ ok: false, code: "stale", message: "" }],
    });
    const { target, cleanup } = render({ agent: dockAgent(), transport });
    try {
      await vi.waitFor(() => expect(buttons(target)).toHaveLength(3));
      buttons(target)[0].click();
      await vi.waitFor(() => expect(target.querySelector(".prompt")?.textContent).toContain("Edit file?"));
      expect(transport.detailCalls.map((c) => c.refresh)).toEqual([true, true]);
      expect(transport.actCalls).toHaveLength(1);
      expect(target.querySelector(".note")?.textContent).toBe(
        "The prompt changed — read the new one before answering.",
      );
    } finally {
      cleanup();
    }
  });

  it("shows Reading the prompt… and re-polls while the runtime reads it", async () => {
    const transport = fakeAgentTransport({
      details: [
        { kind: "ok", detail: agentDetail({ prompt: null, promptPending: true, revision: null, options: [] }) },
        { kind: "ok", detail: agentDetail() },
      ],
    });
    const { target, cleanup } = render({ agent: dockAgent(), transport });
    try {
      await vi.waitFor(() => expect(target.textContent).toContain("Reading the prompt…"));
      await vi.waitFor(() => expect(buttons(target)).toHaveLength(3), { timeout: 2500 });
      expect(transport.detailCalls.map((c) => c.refresh)).toEqual([true, false]);
    } finally {
      cleanup();
    }
  });

  it("explains a refused answer inline", async () => {
    const transport = fakeAgentTransport({ outcomes: [{ ok: false, code: "disconnected", message: "" }] });
    const { target, cleanup } = render({ agent: dockAgent(), transport });
    try {
      await vi.waitFor(() => expect(buttons(target)).toHaveLength(3));
      buttons(target)[2].click();
      await vi.waitFor(() =>
        expect(target.querySelector(".note")?.textContent).toBe("The agent's server is offline."),
      );
      expect(target.textContent).not.toContain("Sent ✓");
    } finally {
      cleanup();
    }
  });

  it("does not force a re-read for an agent that is not blocked", async () => {
    const transport = fakeAgentTransport({
      details: [{ kind: "ok", detail: agentDetail({ status: "working", prompt: null, options: [] }) }],
    });
    const { target, cleanup } = render({ agent: dockAgent({ status: "working" }), transport });
    try {
      await vi.waitFor(() => expect(transport.detailCalls).toHaveLength(1));
      expect(transport.detailCalls[0].refresh).toBe(false);
      expect(buttons(target)).toHaveLength(0);
      expect(target.querySelector(".prompt")).toBeNull();
    } finally {
      cleanup();
    }
  });

  it("opens the pane in herdr and closes", async () => {
    const transport = fakeAgentTransport({ outcomes: [{ ok: true, code: "focused", message: "" }] });
    const { target, onClose, cleanup } = render({ agent: dockAgent({ status: "done" }), transport });
    try {
      const focus = await vi.waitFor(() => {
        const b = target.querySelector<HTMLButtonElement>("button.focus");
        expect(b).not.toBeNull();
        return b!;
      });
      expect(focus.textContent?.trim()).toBe("Open in herdr");
      focus.click();
      await vi.waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
      expect(transport.actCalls[0]).toMatchObject({ action: "focus", ref: { serverId: "prod", paneId: "p0" } });
    } finally {
      cleanup();
    }
  });

  it("says when the agent is gone", async () => {
    const transport = fakeAgentTransport({ details: [{ kind: "gone" }] });
    const { target, cleanup } = render({ agent: dockAgent(), transport });
    try {
      await vi.waitFor(() =>
        expect(target.querySelector(".note")?.textContent).toBe("This agent is no longer available."),
      );
    } finally {
      cleanup();
    }
  });

  it("speaks Czech", async () => {
    setLang("cs");
    const transport = fakeAgentTransport();
    const { target, cleanup } = render({ agent: dockAgent(), transport });
    try {
      await vi.waitFor(() => expect(target.querySelector("button.focus")?.textContent?.trim()).toBe("Otevřít v herdr"));
      expect(target.textContent).toContain("Potřebuje tě");
      expect(target.querySelector("button.close")?.getAttribute("title")).toBe("Zavřít (Esc)");
    } finally {
      cleanup();
    }
  });
});

describe("DockCard (idle list)", () => {
  it("lists idle agents and focuses the one clicked", async () => {
    const transport = fakeAgentTransport({ outcomes: [{ ok: true, code: "focused", message: "" }] });
    const idle = [
      dockAgent({ paneId: "i1", title: "Alpha", status: "idle", sinceS: 30 }),
      dockAgent({ paneId: "i2", title: "Zeta", status: "idle", sinceS: 4000 }),
    ];
    const { target, onClose, cleanup } = render({ idle, transport });
    try {
      expect(target.querySelector(".title")?.textContent).toBe("Idle agents (2)");
      const rows = Array.from(target.querySelectorAll<HTMLButtonElement>("button.row"));
      expect(rows.map((r) => r.querySelector(".row-title")?.textContent)).toEqual(["Alpha", "Zeta"]);
      expect(rows[1].querySelector(".row-since")?.textContent).toBe("1h");
      rows[1].click();
      await vi.waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
      expect(transport.actCalls[0]).toMatchObject({ action: "focus", ref: { serverId: "prod", paneId: "i2" } });
      expect(transport.detailCalls).toHaveLength(0);
    } finally {
      cleanup();
    }
  });
});

describe("DockCard (armed confirm)", () => {
  it("does not carry an armed confirm over to a newly read prompt", async () => {
    const confirmOpt = (label: string) => ({ key: "3", label, id: "deny", kind: "option", confirm: true }) as const;
    const transport = fakeAgentTransport({
      details: [
        { kind: "ok", detail: agentDetail({ options: [confirmOpt("No")] }) },
        { kind: "ok", detail: agentDetail({ options: [confirmOpt("Delete everything")], revision: "rev-2" }) },
      ],
    });
    const state = flushState();
    const target = document.createElement("div");
    document.body.appendChild(target);
    const instance = mount(DockCard, {
      target,
      props: {
        get agent() { return state.agent; },
        sinceOf: (a: DockAgent) => a.sinceS,
        iconUrl: () => null,
        transport,
        onClose: vi.fn(),
      },
    });
    try {
      await vi.waitFor(() => expect(buttons(target)).toHaveLength(1));
      buttons(target)[0].click();
      flushSync();
      expect(buttons(target)[0].textContent?.trim()).toBe("Sure? Click again");
      state.agent = dockAgent({ status: "working" });
      flushSync();
      state.agent = dockAgent({ status: "blocked" });
      await vi.waitFor(() => expect(buttons(target)[0]?.textContent?.trim()).toBe("3. Delete everything"));
      buttons(target)[0].click();
      flushSync();
      expect(transport.actCalls).toHaveLength(0);
    } finally {
      unmount(instance);
      target.remove();
    }
  });
});
