import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { flushSync, mount, tick, unmount } from "svelte";
import HealthNotice from "./HealthNotice.svelte";
import { HEALTH_GRACE_MS, healthLine, type HealthMessages } from "./healthStatus";
import { setLang } from "./i18n.svelte";

const M: HealthMessages = {
  config_error: "config error: {error}",
  runtime_mismatch: "runtime {runtime} ≠ app {app}",
  bridge_mismatch: "bridge {id} {bridge} ≠ runtime {runtime}",
  bridge_protocol: "bridge {id} protocol",
  bridge_token: "bridge {id}: token rejected {since}",
  bridge_down: "bridge {id}: disconnected {since}",
  d200_down: "D200: disconnected {since}",
  d200_locked: "D200 locked by {pid}",
  seconds: "{n} s",
  minutes: "{n} min",
  hours: "{n} h",
};
const NOW = 10_000_000;

describe("healthLine", () => {
  it("puts a config error first, pointing at Maintenance", () => {
    expect(
      healthLine({ config_error: "invalid grid 'wide'", version: "0.9.0", app_version: "0.9.1" }, M, NOW),
    ).toBe("config error: invalid grid 'wide' · runtime 0.9.0 ≠ app 0.9.1");
  });

  it("is empty for a healthy runtime and for an old runtime without the fields", () => {
    expect(healthLine({ ok: true }, M, NOW)).toBe("");
    expect(
      healthLine(
        {
          version: "0.8.1",
          app_version: "0.8.1",
          servers: { local: { connected: true, bridge_version: "0.8.1", since: NOW - 1 } },
          d200: { connected: true, last_frame_at: NOW },
        },
        M,
        NOW,
      ),
    ).toBe("");
  });

  it("reports app/runtime and runtime/bridge version mismatches", () => {
    expect(
      healthLine(
        {
          version: "0.8.0",
          app_version: "0.8.1",
          servers: { box: { connected: true, bridge_version: "0.7.9", protocol_supported: false } },
        },
        M,
        NOW,
      ),
    ).toBe("runtime 0.8.0 ≠ app 0.8.1 · bridge box protocol · bridge box 0.7.9 ≠ runtime 0.8.0");
  });

  it("explains a dropped bridge only after the grace period, token first", () => {
    const fresh = { servers: { local: { connected: false, since: NOW - 1000, last_error: "x" } } };
    expect(healthLine(fresh, M, NOW)).toBe("");
    const token = {
      servers: {
        local: {
          connected: false,
          since: NOW - 3 * 60_000,
          last_error: "token rejected (close 4401) — check token_env/keychain",
        },
      },
    };
    expect(healthLine(token, M, NOW)).toBe("bridge local: token rejected 3 min");
    const down = { servers: { box: { connected: false, since: NOW - HEALTH_GRACE_MS } } };
    expect(healthLine(down, M, NOW)).toBe("bridge box: disconnected 15 s");
  });

  it("stays quiet about a server that never connected, unless its token was rejected", () => {
    const unused = { servers: { t3: { connected: false, ever_connected: false, since: NOW - 3_600_000, last_error: "connection refused" } } };
    expect(healthLine(unused, M, NOW)).toBe("");
    const lost = { servers: { t3: { connected: false, ever_connected: true, since: NOW - HEALTH_GRACE_MS } } };
    expect(healthLine(lost, M, NOW)).toBe("bridge t3: disconnected 15 s");
    const badToken = { servers: { t3: { connected: false, ever_connected: false, since: NOW - 60_000, last_error: "token rejected (close 4401)" } } };
    expect(healthLine(badToken, M, NOW)).toBe("bridge t3: token rejected 1 min");
  });

  it("reports a D200 only once it has been driven, and a foreign lock owner", () => {
    const neverAttached = { d200: { connected: false, last_frame_at: null, since: 0, last_error: "no device" } };
    expect(healthLine(neverAttached, M, NOW)).toBe("");
    const lost = { d200: { connected: false, last_frame_at: 5, since: NOW - 2 * 3_600_000 } };
    expect(healthLine(lost, M, NOW)).toBe("D200: disconnected 2 h");
    expect(healthLine({ d200: { connected: false, lock_owner: 4242 } }, M, NOW)).toBe(
      "D200 locked by 4242",
    );
  });
});

describe("HealthNotice", () => {
  let target: HTMLElement;
  beforeEach(() => {
    target = document.createElement("div");
    document.body.appendChild(target);
  });
  afterEach(() => {
    target.remove();
    setLang("en");
  });

  async function render(payload: unknown, lang: "en" | "cs") {
    setLang(lang);
    const instance = mount(HealthNotice, {
      target,
      props: { fetchHealth: async () => payload, intervalMs: 60_000 },
    });
    for (let i = 0; i < 5; i += 1) await tick();
    flushSync();
    return () => unmount(instance);
  }

  it("renders the mismatch in English and Czech", async () => {
    const payload = { version: "0.8.0", app_version: "0.8.1" };
    let cleanup = await render(payload, "en");
    expect(target.textContent).toContain("runtime 0.8.0 ≠ app 0.8.1 — restart the runtime");
    cleanup();
    cleanup = await render(payload, "cs");
    expect(target.textContent).toContain("runtime 0.8.0 ≠ aplikace 0.8.1 — restartuj runtime");
    cleanup();
  });

  it("renders the runtime's config error in English and Czech", async () => {
    const payload = { source: "config_error", config_error: "bridge token for server 'local' not found" };
    let cleanup = await render(payload, "en");
    expect(target.textContent).toContain(
      "config error: bridge token for server 'local' not found — see Maintenance",
    );
    cleanup();
    cleanup = await render(payload, "cs");
    expect(target.textContent).toContain(
      "chyba configu: bridge token for server 'local' not found — viz Údržba",
    );
    cleanup();
  });

  it("renders nothing while the deck is healthy", async () => {
    const cleanup = await render({ version: "0.8.1", app_version: "0.8.1" }, "en");
    expect(target.querySelector(".health-notice")).toBeNull();
    cleanup();
  });
});
