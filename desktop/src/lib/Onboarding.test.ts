import { afterEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount, type ComponentProps } from "svelte";

import Onboarding from "./Onboarding.svelte";
import { setLang } from "./i18n.svelte";
import { reactiveProps } from "./testProps.svelte";
import type { ConnectRequest, SetupStatus, SetupTransport } from "./onboardingClient";

function status(overrides: Partial<SetupStatus> = {}): SetupStatus {
  return {
    mode: "local",
    connected: false,
    reason: "first_run",
    localHerdrAvailable: true,
    savedRemoteAvailable: false,
    choice: null,
    socketPath: "/tmp/herdr.sock",
    localSessions: [{ name: "personal", serverId: "local:personal", socketPath: "/tmp/herdr.sock", available: true, selected: false }],
    connections: {},
    ...overrides,
  };
}

function fakeTransport(): SetupTransport & { calls: ConnectRequest[] } {
  const calls: ConnectRequest[] = [];
  return {
    calls,
    status: async () => null,
    connect: async (req) => {
      calls.push(req);
      return { ok: false, connected: false, error: "unreachable", code: null };
    },
  };
}

let cleanup: (() => void) | null = null;
afterEach(() => {
  cleanup?.();
  cleanup = null;
});

function render(props: Record<string, unknown>) {
  setLang("en");
  const target = document.createElement("div");
  document.body.appendChild(target);
  const props$ = reactiveProps<Record<string, unknown>>({ onConnected: () => {}, transport: fakeTransport(), ...props });
  const instance = mount(Onboarding, { target, props: props$ as unknown as ComponentProps<typeof Onboarding> });
  flushSync();
  cleanup = () => { unmount(instance); target.remove(); };
  return { target, props: props$ };
}

describe("Onboarding card", () => {
  it("disables 'Save and connect' while nothing is selected", () => {
    const { target } = render({ view: "welcome", status: status() });
    const save = Array.from(target.querySelectorAll<HTMLButtonElement>("button"))
      .find((b) => b.textContent?.includes("Save and connect"))!;
    expect(save.disabled).toBe(true);
    const box = target.querySelector<HTMLInputElement>(".session-list input[type='checkbox']")!;
    box.checked = true;
    box.dispatchEvent(new Event("change", { bubbles: true }));
    flushSync();
    expect(save.disabled).toBe(false);
  });

  it("does not claim a saved bridge runs over Tailscale", () => {
    const { target } = render({ view: "welcome", status: status({ savedRemoteAvailable: true }) });
    expect(target.textContent).not.toContain("Tailscale");
    expect(target.textContent).toContain("from your config");
  });

  it("auto-reconnects again when herdr comes back a second time", async () => {
    vi.useFakeTimers();
    try {
      const transport = fakeTransport();
      const down = status({ reason: "local_unavailable", choice: "local", localHerdrAvailable: false, localSessions: [] });
      const up = status({ choice: "local", localSessions: [] });
      const { props } = render({ view: "reconnect", status: up, transport });
      await vi.advanceTimersByTimeAsync(0);
      expect(transport.calls).toHaveLength(1);

      props.status = down;
      flushSync();
      props.status = up;
      flushSync();
      // First retry waits out the backoff (2s after one attempt).
      await vi.advanceTimersByTimeAsync(2_100);
      flushSync();
      expect(transport.calls).toHaveLength(2);
    } finally {
      vi.useRealTimers();
    }
  });
});
