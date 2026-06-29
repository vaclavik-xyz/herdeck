# Phase 3c-ii Onboarding Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the first-run onboarding card to the floating-deck window — it polls `GET /setup`, shows a welcome/reconnect card while the user must act, flips to the deck once connected, and is reachable again after first run via a "change connection" affordance.

**Architecture:** Mirror the existing desktop split: all logic lives in a framework-free, Vitest-tested `onboardingClient.ts` (status parse + the render decision + a Tauri-`invoke` transport over the `setup_status`/`setup_connect` commands that already exist from 3c-i); `Onboarding.svelte` is a thin template over it (compile-smoke only, like the field widgets); `App.svelte` wires a setup-status poll and switches between `<Onboarding>` and `<DeckView>`. The access token never reaches JS — the Rust commands inject it, exactly like the deck/config proxies.

**Tech Stack:** Svelte 5 (runes: `$state`/`$derived`/`$props`/`$effect`), TypeScript, Vitest (`npm test`), Vite (`npm run build`), `@tauri-apps/api` `invoke`.

## Global Constraints

- All commands run from `desktop/`. Tests: `npm test` (vitest run). Build/compile-check: `npm run build` (vite build — compiles every `.svelte`, fails on a type/compile error).
- Code & commit messages in English; conventional-commit format; **NO `Co-Authored-By` trailer**; commit locally only (do NOT push).
- The two Tauri commands already exist (3c-i Task 9): `invoke("setup_status")` → the `GET /setup` JSON `{mode, connected, reason, local_herdr_available, choice, socket_path}`; `invoke("setup_connect", { body })` where `body` is `{choice:"local"|"demo"|"remote", url?, token?, id?}` → `{ok, connected?, error?}` on success (the command throws on a non-200). The token VALUE the user types goes into `body` and is forwarded by Rust; it is **never read back, logged, or stored in JS**.
- Reuse, do not re-declare, the `InvokeFn` type already exported from `src/lib/deckClient.ts`.
- Render decision is **exhaustive on `reason`** and defaults to showing the deck, so no setup state can trap the user behind a card that does not apply: `reason === "first_run"` → welcome card; `reason === "local_unavailable"` → reconnect card; everything else (`null` connected, `mock_env`, `demo`, or anything unknown, or an unreadable status) → the deck.
- `.svelte` components are tested compile-smoke only (import → `toBeTruthy`), matching `src/lib/fields/widgets.smoke.test.ts`. Behavioral logic must live in `onboardingClient.ts` and be unit-tested there.
- Follow existing style: framework-free `.ts` helpers narrow `unknown` (snake_case JSON → camelCase fields), transports are injected for testability, Svelte 5 runes throughout.

## File Structure

- **Create** `src/lib/onboardingClient.ts` — pure status parse (`SetupStatus`/`parseSetupStatus`), the render decision (`OnboardingView`/`onboardingDecision`/`shouldOnboard`), the connect request/result shapes (`ConnectRequest`/`ConnectResult`/`parseConnectResult`), and the injected transport (`SetupTransport`/`setupTransport`). One file: these change together and all belong to "talking to /setup".
- **Create** `src/lib/onboardingClient.test.ts` — Vitest unit tests for everything in the client.
- **Create** `src/lib/Onboarding.svelte` — the card. Props: `view` (`"welcome"|"reconnect"`), `status`, `transport`, `onConnected`, `onDismiss?`. Local-only form state; calls `transport.connect`; shows inline errors. Thin template; no logic beyond binding + calling the client.
- **Create** `src/lib/onboarding.smoke.test.ts` — compile-smoke for `Onboarding.svelte`.
- **Modify** `src/App.svelte` — add the setup-status poll, the `<Onboarding>` vs `<DeckView>` switch, and the "change connection" affordance for re-onboarding.

---

## Task 1: onboardingClient — status parse + render decision

**Files:**
- Create: `src/lib/onboardingClient.ts`
- Test: `src/lib/onboardingClient.test.ts`

**Interfaces:**
- Consumes: nothing (pure).
- Produces:
  - `interface SetupStatus { mode: string; connected: boolean; reason: string | null; localHerdrAvailable: boolean; choice: string | null; socketPath: string }`
  - `function parseSetupStatus(raw: unknown): SetupStatus | null`
  - `type OnboardingView = "deck" | "welcome" | "reconnect"`
  - `function onboardingDecision(status: SetupStatus | null): OnboardingView`
  - `function shouldOnboard(status: SetupStatus | null, override: boolean): OnboardingView` — folds the manual re-onboarding override into the decision (used by `App.svelte`).

- [ ] **Step 1: Write the failing test**

Create `src/lib/onboardingClient.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import {
  parseSetupStatus,
  onboardingDecision,
  shouldOnboard,
  type SetupStatus,
} from "./onboardingClient";

const full = {
  mode: "remote",
  connected: true,
  reason: null,
  local_herdr_available: false,
  choice: null,
  socket_path: "/home/u/.config/herdr/herdr.sock",
};

describe("parseSetupStatus", () => {
  it("shapes a full status (snake_case -> camelCase)", () => {
    const s = parseSetupStatus(full);
    expect(s).not.toBeNull();
    expect(s?.mode).toBe("remote");
    expect(s?.connected).toBe(true);
    expect(s?.reason).toBeNull();
    expect(s?.localHerdrAvailable).toBe(false);
    expect(s?.choice).toBeNull();
    expect(s?.socketPath).toBe("/home/u/.config/herdr/herdr.sock");
  });

  it("keeps a string reason and a local_herdr_available true", () => {
    const s = parseSetupStatus({ ...full, reason: "first_run", local_herdr_available: true });
    expect(s?.reason).toBe("first_run");
    expect(s?.localHerdrAvailable).toBe(true);
  });

  it("returns null for non-objects / missing mode", () => {
    expect(parseSetupStatus(null)).toBeNull();
    expect(parseSetupStatus(undefined)).toBeNull();
    expect(parseSetupStatus("nope")).toBeNull();
    expect(parseSetupStatus({ connected: true })).toBeNull(); // no mode
  });

  it("defaults soft fields when absent or wrong-typed", () => {
    const s = parseSetupStatus({ mode: "mock" });
    expect(s).not.toBeNull();
    expect(s?.connected).toBe(false);
    expect(s?.reason).toBeNull();
    expect(s?.localHerdrAvailable).toBe(false);
    expect(s?.choice).toBeNull();
    expect(s?.socketPath).toBe("");
  });
});

describe("onboardingDecision (exhaustive on reason, defaults to deck)", () => {
  const at = (reason: string | null): SetupStatus => ({
    mode: "mock",
    connected: false,
    reason,
    localHerdrAvailable: true,
    choice: null,
    socketPath: "",
  });

  it("first_run -> welcome", () => {
    expect(onboardingDecision(at("first_run"))).toBe("welcome");
  });

  it("local_unavailable -> reconnect", () => {
    expect(onboardingDecision(at("local_unavailable"))).toBe("reconnect");
  });

  it("connected/null, demo, mock_env, unknown -> deck", () => {
    expect(onboardingDecision(at(null))).toBe("deck");
    expect(onboardingDecision(at("demo"))).toBe("deck");
    expect(onboardingDecision(at("mock_env"))).toBe("deck");
    expect(onboardingDecision(at("something_new"))).toBe("deck");
  });

  it("null status (not ready / unreadable) -> deck", () => {
    expect(onboardingDecision(null)).toBe("deck");
  });
});

describe("shouldOnboard (manual re-onboarding override)", () => {
  const demo: SetupStatus = {
    mode: "mock",
    connected: false,
    reason: "demo",
    localHerdrAvailable: true,
    choice: "demo",
    socketPath: "",
  };

  it("forces the welcome card over a deck decision when override is set", () => {
    expect(shouldOnboard(demo, false)).toBe("deck");
    expect(shouldOnboard(demo, true)).toBe("welcome");
  });

  it("keeps a real reconnect card even under override", () => {
    const recon = { ...demo, reason: "local_unavailable" };
    expect(shouldOnboard(recon, true)).toBe("reconnect");
    expect(shouldOnboard(recon, false)).toBe("reconnect");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/admin/projects/herdeck/desktop && npm test -- onboardingClient`
Expected: FAIL — cannot resolve `./onboardingClient`.

- [ ] **Step 3: Write minimal implementation**

Create `src/lib/onboardingClient.ts`:

```ts
// Framework-free helpers for the first-run onboarding card. Like sidecar.ts /
// deckClient.ts, these narrow `unknown` (the raw /setup JSON) and inject the
// Tauri transport, so the whole decision/parse logic is unit-testable under
// Vitest without a Tauri WebView. Onboarding.svelte is a thin template over this.
//
// The access token is NEVER here: the Rust `setup_status` / `setup_connect`
// commands inject it server-side (loopback), exactly like the deck/config
// proxies. A typed remote token flows OUT through `connect` and is never read
// back.

/** Shaped `GET /setup` status (snake_case JSON -> camelCase). `reason` is one of
 *  "mock_env" | "demo" | "first_run" | "local_unavailable" | null. */
export interface SetupStatus {
  mode: string; // "mock" | "local" | "remote"
  connected: boolean;
  reason: string | null;
  localHerdrAvailable: boolean;
  choice: string | null; // "local" | "demo" | null
  socketPath: string;
}

/** Narrow a raw `setup_status` result into a SetupStatus, or null when it is not
 *  a usable status object (treated by the caller as "not ready" -> show the deck). */
export function parseSetupStatus(raw: unknown): SetupStatus | null {
  if (raw == null || typeof raw !== "object") return null;
  const v = raw as Record<string, unknown>;
  if (typeof v.mode !== "string") return null;
  return {
    mode: v.mode,
    connected: v.connected === true,
    reason: typeof v.reason === "string" ? v.reason : null,
    localHerdrAvailable: v.local_herdr_available === true,
    choice: typeof v.choice === "string" ? v.choice : null,
    socketPath: typeof v.socket_path === "string" ? v.socket_path : "",
  };
}

/** Which surface the deck window should show. */
export type OnboardingView = "deck" | "welcome" | "reconnect";

/** The render decision, EXHAUSTIVE on `reason` and defaulting to the deck so no
 *  setup state can trap the user behind a card that does not apply. */
export function onboardingDecision(status: SetupStatus | null): OnboardingView {
  if (!status) return "deck";
  if (status.reason === "first_run") return "welcome";
  if (status.reason === "local_unavailable") return "reconnect";
  return "deck"; // connected (null), demo, mock_env, or anything unknown
}

/** Fold the manual "change connection" override into the decision: when the user
 *  asked to re-onboard and the status would otherwise show the deck, present the
 *  full welcome card so they can pick a new mode (incl. remote). A genuine
 *  reconnect state still wins (it already needs the card). */
export function shouldOnboard(status: SetupStatus | null, override: boolean): OnboardingView {
  const decision = onboardingDecision(status);
  if (override && decision === "deck") return "welcome";
  return decision;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/admin/projects/herdeck/desktop && npm test -- onboardingClient`
Expected: PASS (all Task 1 tests).

- [ ] **Step 5: Commit**

```bash
git add src/lib/onboardingClient.ts src/lib/onboardingClient.test.ts
git commit -m "feat(desktop): onboarding status parse + render decision"
```

---

## Task 2: onboardingClient — connect requests + injected transport

**Files:**
- Modify: `src/lib/onboardingClient.ts`
- Modify: `src/lib/onboardingClient.test.ts`

**Interfaces:**
- Consumes: `InvokeFn` from `./deckClient`; `SetupStatus`/`parseSetupStatus` from Task 1.
- Produces:
  - `type ConnectRequest = { choice: "local" } | { choice: "demo" } | { choice: "remote"; url: string; token: string; id?: string }`
  - `interface ConnectResult { ok: boolean; connected: boolean; error: string | null }`
  - `function parseConnectResult(raw: unknown): ConnectResult`
  - `interface SetupTransport { status(): Promise<SetupStatus | null>; connect(req: ConnectRequest): Promise<ConnectResult> }`
  - `function setupTransport(invoke: InvokeFn): SetupTransport`

- [ ] **Step 1: Write the failing test**

Append to `src/lib/onboardingClient.test.ts`:

```ts
import {
  parseConnectResult,
  setupTransport,
  type ConnectRequest,
} from "./onboardingClient";

describe("parseConnectResult", () => {
  it("shapes a success", () => {
    const r = parseConnectResult({ ok: true, connected: true });
    expect(r).toEqual({ ok: true, connected: true, error: null });
  });

  it("shapes a failure with an error reason", () => {
    const r = parseConnectResult({ ok: false, error: "bad_token" });
    expect(r).toEqual({ ok: false, connected: false, error: "bad_token" });
  });

  it("treats garbage as a non-ok result (never throws)", () => {
    expect(parseConnectResult(null)).toEqual({ ok: false, connected: false, error: null });
    expect(parseConnectResult("nope")).toEqual({ ok: false, connected: false, error: null });
  });
});

describe("setupTransport", () => {
  it("status() invokes setup_status and parses the result", async () => {
    const calls: { cmd: string; args?: Record<string, unknown> }[] = [];
    const invoke = async (cmd: string, args?: Record<string, unknown>) => {
      calls.push({ cmd, args });
      return { mode: "mock", reason: "first_run", local_herdr_available: true };
    };
    const t = setupTransport(invoke);
    const s = await t.status();
    expect(calls).toEqual([{ cmd: "setup_status", args: undefined }]);
    expect(s?.mode).toBe("mock");
    expect(s?.reason).toBe("first_run");
  });

  it("status() returns null when invoke rejects (outside the WebView)", async () => {
    const invoke = async () => {
      throw new Error("no tauri");
    };
    expect(await setupTransport(invoke).status()).toBeNull();
  });

  it("connect() forwards the request as the `body` arg and parses the result", async () => {
    const calls: { cmd: string; args?: Record<string, unknown> }[] = [];
    const invoke = async (cmd: string, args?: Record<string, unknown>) => {
      calls.push({ cmd, args });
      return { ok: true, connected: true };
    };
    const req: ConnectRequest = { choice: "remote", url: "ws://h:8788", token: "tok", id: "herdr" };
    const r = await setupTransport(invoke).connect(req);
    expect(calls).toEqual([{ cmd: "setup_connect", args: { body: req } }]);
    expect(r).toEqual({ ok: true, connected: true, error: null });
  });

  it("connect() surfaces a thrown command (non-200) as a non-ok result", async () => {
    const invoke = async () => {
      throw new Error("sidecar returned HTTP 503 for /setup/connect");
    };
    const r = await setupTransport(invoke).connect({ choice: "demo" });
    expect(r.ok).toBe(false);
    expect(r.error).toContain("HTTP 503");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/admin/projects/herdeck/desktop && npm test -- onboardingClient`
Expected: FAIL — `parseConnectResult` / `setupTransport` not exported.

- [ ] **Step 3: Write minimal implementation**

Append to `src/lib/onboardingClient.ts` (add the `InvokeFn` import at the top, next to the other imports — there are none yet, so add an import line below the file's header comment):

```ts
import type { InvokeFn } from "./deckClient";
```

Then append at the end of the file:

```ts
/** The shape POSTed to `setup_connect` as `body`. The remote variant carries the
 *  user-typed token (forwarded by Rust, never read back). */
export type ConnectRequest =
  | { choice: "local" }
  | { choice: "demo" }
  | { choice: "remote"; url: string; token: string; id?: string };

/** Shaped `/setup/connect` result. `ok` gates the flip-to-deck; `error` is the
 *  inline reason on failure (bad_token / unreachable / bad url / a thrown HTTP). */
export interface ConnectResult {
  ok: boolean;
  connected: boolean;
  error: string | null;
}

/** Narrow a raw connect result; never throws (garbage -> a non-ok result). */
export function parseConnectResult(raw: unknown): ConnectResult {
  if (raw == null || typeof raw !== "object") {
    return { ok: false, connected: false, error: null };
  }
  const v = raw as Record<string, unknown>;
  return {
    ok: v.ok === true,
    connected: v.connected === true,
    error: typeof v.error === "string" ? v.error : null,
  };
}

/** How the onboarding card talks to the sidecar. Injected so the card stays
 *  framework-free and is testable with a fake, and so the real transport (the
 *  two token-injecting Tauri commands) lives in one place. */
export interface SetupTransport {
  /** `setup_status` -> the parsed status, or null when unavailable/unreadable. */
  status(): Promise<SetupStatus | null>;
  /** `setup_connect({ body })` -> the parsed result. A thrown command (non-200 /
   *  no WebView) becomes a non-ok result carrying the message, so the card can
   *  show it inline rather than crashing. */
  connect(req: ConnectRequest): Promise<ConnectResult>;
}

/** Production transport over the Tauri commands. `setup_status` takes no args;
 *  `setup_connect` takes the request as `body` (matching the Rust signature). */
export function setupTransport(invoke: InvokeFn): SetupTransport {
  return {
    async status() {
      try {
        return parseSetupStatus(await invoke("setup_status"));
      } catch {
        return null;
      }
    },
    async connect(req) {
      try {
        return parseConnectResult(await invoke("setup_connect", { body: req }));
      } catch (e) {
        return { ok: false, connected: false, error: e instanceof Error ? e.message : String(e) };
      }
    },
  };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/admin/projects/herdeck/desktop && npm test -- onboardingClient`
Expected: PASS (Task 1 + Task 2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/lib/onboardingClient.ts src/lib/onboardingClient.test.ts
git commit -m "feat(desktop): setup connect requests + token-injecting transport"
```

---

## Task 3: Onboarding.svelte — the welcome / reconnect card

**Files:**
- Create: `src/lib/Onboarding.svelte`
- Create: `src/lib/onboarding.smoke.test.ts`

**Interfaces:**
- Consumes: `SetupStatus`, `SetupTransport`, `ConnectRequest`, `ConnectResult` from `./onboardingClient`.
- Produces: the `<Onboarding>` component with props
  `{ view: "welcome" | "reconnect"; status: SetupStatus | null; transport: SetupTransport | null; onConnected: () => void; onDismiss?: () => void }`.

- [ ] **Step 1: Write the failing compile-smoke test**

Create `src/lib/onboarding.smoke.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import Onboarding from "./Onboarding.svelte";

// Compile-smoke only (matches fields/widgets.smoke.test.ts): importing the
// .svelte compiles it, catching syntax/compile errors without a render harness.
// The card's behavior lives in onboardingClient.ts and is unit-tested there.
describe("Onboarding compile-smoke", () => {
  it("compiles the onboarding card", () => {
    expect(Onboarding).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/admin/projects/herdeck/desktop && npm test -- onboarding.smoke`
Expected: FAIL — cannot resolve `./Onboarding.svelte`.

- [ ] **Step 3: Write minimal implementation**

Create `src/lib/Onboarding.svelte`:

```svelte
<script lang="ts">
  // First-run (and re-onboarding) card for the floating-deck window. A thin
  // template over onboardingClient.ts: it binds form state and calls
  // transport.connect; all decision/parse logic lives in the client. The token
  // field is a plain password input whose value goes straight into the connect
  // request and is never read back.
  import type {
    SetupStatus,
    SetupTransport,
    ConnectRequest,
  } from "./onboardingClient";

  let {
    view,
    status,
    transport,
    onConnected,
    onDismiss = undefined,
  }: {
    view: "welcome" | "reconnect";
    status: SetupStatus | null;
    transport: SetupTransport | null;
    onConnected: () => void;
    onDismiss?: (() => void) | undefined;
  } = $props();

  let showRemote = $state(false);
  let url = $state("");
  let token = $state("");
  let serverId = $state("");
  let busy = $state(false);
  let error = $state<string | null>(null);

  const localAvailable = $derived(status?.localHerdrAvailable === true);

  async function run(req: ConnectRequest): Promise<void> {
    if (!transport || busy) return;
    busy = true;
    error = null;
    const r = await transport.connect(req);
    busy = false;
    if (r.ok) {
      onConnected();
    } else {
      error = r.error ?? "Připojení selhalo.";
    }
  }

  function connectLocal(): void {
    void run({ choice: "local" });
  }
  function connectDemo(): void {
    void run({ choice: "demo" });
  }
  function connectRemote(): void {
    const u = url.trim();
    if (!u || !token) {
      error = "Vyplň URL i token.";
      return;
    }
    const req: ConnectRequest = { choice: "remote", url: u, token };
    const id = serverId.trim();
    if (id) (req as { id?: string }).id = id;
    void run(req);
  }
</script>

<section class="onboarding">
  {#if view === "reconnect"}
    <h1>herdr neběží</h1>
    <p class="lead">Lokální připojení je zapamatované, ale herdr teď neběží.</p>
    <div class="actions">
      <button class="primary" disabled={busy} onclick={connectLocal}>Zkusit znovu</button>
      <button class="link" disabled={busy} onclick={() => (showRemote = !showRemote)}>
        Připojit vzdáleně…
      </button>
    </div>
  {:else}
    <h1>Připojit herdeck</h1>
    {#if localAvailable}
      <p class="lead ok">✓ herdr běží lokálně</p>
      <div class="actions">
        <button class="primary" disabled={busy} onclick={connectLocal}>Připojit</button>
        <button class="link" disabled={busy} onclick={() => (showRemote = !showRemote)}>
          Vzdálený herdr…
        </button>
      </div>
    {:else}
      <p class="lead">herdr nebyl lokálně nalezen — spusť ho, nebo se připoj vzdáleně.</p>
      <div class="actions">
        <button class="primary" disabled={busy} onclick={() => (showRemote = true)}>
          Vzdálený herdr…
        </button>
      </div>
    {/if}
  {/if}

  {#if showRemote || (view === "welcome" && !localAvailable)}
    <form class="remote" onsubmit={(e) => { e.preventDefault(); connectRemote(); }}>
      <label>URL<input type="text" placeholder="ws(s)://host:8788" bind:value={url} /></label>
      <label>Token<input type="password" bind:value={token} /></label>
      <label class="adv">ID (volitelné)<input type="text" placeholder="herdr" bind:value={serverId} /></label>
      <button class="primary" type="submit" disabled={busy}>Připojit</button>
    </form>
  {/if}

  <div class="footer">
    {#if view === "welcome"}
      <button class="link" disabled={busy} onclick={connectDemo}>Prozkoumat demo</button>
    {/if}
    {#if onDismiss}
      <button class="link dismiss" disabled={busy} onclick={onDismiss}>← zpět na deck</button>
    {/if}
  </div>

  {#if error}<p class="error" role="alert">{error}</p>{/if}
</section>

<style>
  .onboarding {
    box-sizing: border-box;
    min-height: 100vh;
    padding: 24px 18px;
    background: #0b0b0d;
    color: #e7ecf3;
    font: 13px/1.4 system-ui, -apple-system, sans-serif;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  h1 {
    margin: 0;
    font-size: 17px;
  }
  .lead {
    margin: 0;
    color: #8b97a4;
  }
  .lead.ok {
    color: #3fb950;
  }
  .actions,
  .footer {
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
  }
  .remote {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 10px;
    border-radius: 10px;
    background: #17171b;
  }
  .remote label {
    display: flex;
    flex-direction: column;
    gap: 3px;
    font-size: 11px;
    color: #8b97a4;
  }
  .remote input {
    padding: 6px 8px;
    border-radius: 6px;
    border: 1px solid #2a2a2e;
    background: #0b0b0d;
    color: #e7ecf3;
    font: inherit;
  }
  button.primary {
    padding: 7px 14px;
    border: none;
    border-radius: 7px;
    background: #2563eb;
    color: #fff;
    font: inherit;
    cursor: pointer;
  }
  button.primary:disabled {
    opacity: 0.5;
    cursor: default;
  }
  button.link {
    border: none;
    background: none;
    color: #5af;
    cursor: pointer;
    font: inherit;
    padding: 4px 0;
  }
  button.link.dismiss {
    margin-left: auto;
    color: #8b97a4;
  }
  .error {
    margin: 0;
    color: #f0883e;
  }
</style>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/admin/projects/herdeck/desktop && npm test -- onboarding.smoke`
Expected: PASS (the import compiles the component).

- [ ] **Step 5: Verify the whole frontend still compiles**

Run: `cd /Users/admin/projects/herdeck/desktop && npm run build`
Expected: build succeeds (no Svelte/TS compile errors).

- [ ] **Step 6: Commit**

```bash
git add src/lib/Onboarding.svelte src/lib/onboarding.smoke.test.ts
git commit -m "feat(desktop): onboarding card (welcome/reconnect, local/remote/demo)"
```

---

## Task 4: App.svelte — poll, switch, and re-onboarding affordance

**Files:**
- Modify: `src/App.svelte`

**Interfaces:**
- Consumes: `setupTransport`, `shouldOnboard`, `parseSetupStatus`-backed `SetupStatus`, `SetupTransport` from `./lib/onboardingClient`; the existing `commandTransport`/`asDiscovery`/`Discovery`/`DeckView`.
- Produces: the wired deck window (no new exports).

- [ ] **Step 1: Replace `App.svelte` with the wired version**

This task edits an existing file with no unit test (App imports `@tauri-apps/api`, like today's untested App); it is verified by `npm run build` (compile) plus the already-tested client logic. Replace the entire contents of `src/App.svelte` with:

```svelte
<script lang="ts">
  import { onMount } from "svelte";
  import { invoke } from "@tauri-apps/api/core";
  import { listen } from "@tauri-apps/api/event";
  import DeckView from "./lib/DeckView.svelte";
  import Onboarding from "./lib/Onboarding.svelte";
  import { asDiscovery, type Discovery } from "./lib/sidecar";
  import { commandTransport } from "./lib/deckClient";
  import {
    setupTransport,
    shouldOnboard,
    type SetupStatus,
  } from "./lib/onboardingClient";

  let discovery = $state<Discovery | null>(null);
  let status = $state<SetupStatus | null>(null);
  // Manual "change connection" override: open the welcome card even when the
  // status would show the deck (so a demo/local-pinned user can re-onboard).
  let reonboard = $state(false);

  // The deck reaches the sidecar through token-free Tauri proxies; the setup
  // transport uses the two token-injecting setup commands. Both need discovery
  // first (the Rust commands resolve the sidecar from it).
  const transport = $derived(
    discovery ? commandTransport((cmd, args) => invoke(cmd, args)) : null,
  );
  const setup = $derived(
    discovery ? setupTransport((cmd, args) => invoke(cmd, args)) : null,
  );

  // Which surface to show. Defaults to the deck so no setup state traps the user.
  const view = $derived(shouldOnboard(status, reonboard));

  async function pullDiscovery(): Promise<void> {
    try {
      const d = asDiscovery(await invoke("get_discovery"));
      if (d) discovery = d;
    } catch {
      // Not in a Tauri WebView (plain browser): leave null, DeckView goes offline.
    }
  }

  onMount(() => {
    let alive = true;

    void listen<Discovery>("discovery", (event) => {
      const d = asDiscovery(event.payload);
      if (d) discovery = d;
    });

    // Retry discovery until the supervised sidecar has printed its first line.
    void (async () => {
      while (alive && !discovery) {
        await pullDiscovery();
        if (!discovery) await new Promise((r) => setTimeout(r, 400));
      }
    })();

    // Poll /setup once discovery is up. A few seconds is enough: after a
    // successful connect (and once the source swap settles) the next poll flips
    // the card to the deck without a manual refresh.
    void (async () => {
      while (alive) {
        if (setup) status = await setup.status();
        await new Promise((r) => setTimeout(r, status ? 2500 : 600));
      }
    })();

    return () => {
      alive = false;
    };
  });

  function onConnected(): void {
    reonboard = false;
    // Re-poll promptly so the card flips as soon as the swap settles.
    void (async () => {
      if (setup) status = await setup.status();
    })();
  }
</script>

<main>
  {#if view === "deck"}
    <DeckView {transport} />
    <!-- Re-onboarding affordance: reachable beyond first run, so a user pinned by
         a demo/local marker can switch connection (the backend /setup/connect is
         not first-run-gated). -->
    <button
      class="reonboard"
      title="Změnit připojení"
      aria-label="Změnit připojení"
      onclick={() => (reonboard = true)}>⚙</button
    >
  {:else}
    <Onboarding
      {view}
      {status}
      transport={setup}
      {onConnected}
      onDismiss={reonboard ? () => (reonboard = false) : undefined}
    />
  {/if}
</main>

<style>
  :global(html, body) {
    margin: 0;
    background: #0b0b0d;
  }
  main {
    position: relative;
    width: 100vw;
    min-height: 100vh;
    box-sizing: border-box;
  }
  .reonboard {
    position: fixed;
    left: 8px;
    bottom: 8px;
    width: 22px;
    height: 22px;
    padding: 0;
    border: none;
    border-radius: 6px;
    background: #17171b;
    color: #8b97a4;
    font-size: 12px;
    line-height: 22px;
    cursor: pointer;
    opacity: 0.55;
  }
  .reonboard:hover {
    opacity: 1;
  }
</style>
```

- [ ] **Step 2: Verify the frontend compiles and all tests pass**

Run: `cd /Users/admin/projects/herdeck/desktop && npm run build`
Expected: build succeeds.

Run: `cd /Users/admin/projects/herdeck/desktop && npm test`
Expected: PASS — the full Vitest suite (onboardingClient + onboarding.smoke + the existing sidecar/deckClient/configClient/widgets tests), no regressions.

- [ ] **Step 3: Commit**

```bash
git add src/App.svelte
git commit -m "feat(desktop): poll /setup and switch deck window between onboarding and deck"
```

---

## Manual verification (not automatable HW-free)

After merge, on a Mac the user runs the app (`npm run tauri dev` or a `tauri build` .app):
- Fresh config (no `[[servers]]`, no marker) → the welcome card shows; "Prozkoumat demo" flips to the mock deck; the ⚙ affordance reopens the card; "Vzdálený herdr…" connects to a real bridge.
- With a running local herdr → "✓ herdr běží lokálně" + "Připojit" flips to the live deck.

This is the same class of manual gate as the rest of the desktop app (`tauri build` on a Mac); it is out of scope for the HW-free automated suite.

## Out of scope / follow-ups

- A **tray menu "Change connection…"** item (an alternative re-onboarding entry point) belongs with the tray/hotkeys work in Phase 3d; this slice ships the in-window ⚙ affordance, which already satisfies "reachable beyond first run."
- A render/interaction test harness for `.svelte` components (the repo currently has none) is not introduced here — behavior is unit-tested in `onboardingClient.ts`, components are compile-smoke only, matching the existing convention.

## Self-Review

- **Spec coverage:** welcome/reconnect/deck switch exhaustive on `reason` (Task 1 `onboardingDecision` + tests) ✓; card actions local/remote/demo with inline errors (Task 3) ✓; password token never read back (Task 3 form + Task 2 one-way `connect`) ✓; poll flips card→deck after connect (Task 4 poll + `onConnected`) ✓; re-onboarding beyond first run via ⚙ affordance + `shouldOnboard` override + `onDismiss` (Task 1 + Task 4) ✓; component boundaries (`onboardingClient.ts` tested, `Onboarding.svelte` thin, `App.svelte` wires) ✓; token never in JS (Rust-injected commands) ✓.
- **Placeholder scan:** none — every step has complete code.
- **Type consistency:** `SetupStatus`/`OnboardingView`/`ConnectRequest`/`ConnectResult`/`SetupTransport` defined in Tasks 1-2 and used unchanged in Tasks 3-4; `InvokeFn` reused from `deckClient.ts`; `invoke("setup_connect", { body })` matches the Rust `setup_connect(body)` signature.
