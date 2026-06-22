# Herdeck Elgato Stream Deck plugin — design

- **Date:** 2026-06-22
- **Status:** Approved (brainstorming)

## Goal

Deliver herdeck's agent control as a **native Elgato Stream Deck plugin**: see live
herdr agent status on physical keys and **act** on a blocked agent (Approve / Deny /
Stop) plus focus — **locally and remotely over Tailscale** — installed via the
Elgato ecosystem (double-clickable `.streamDeckPlugin`, coexists with the Stream
Deck app, any deck size).

## Product direction

The value here is **reach and distribution**, not new capability — herdeck already
does control + remote. A native Elgato plugin lowers install friction to a double
click and rides the Stream Deck ecosystem (mix with other keys/profiles, no "close
the official app", publishable to the Marketplace).

The differentiator versus existing herdr Stream Deck plugins (e.g.
timvdhoorn/stream-deck-herdr-plugin) is that herdeck's plugin can **act** on agents
and reach **remote** servers, not just mirror status and focus. A read-only mirror
would be a strictly worse clone, so v1 is control-capable.

Architecture principle: **the brain stays in herdeck's tested Python core; the
Elgato side is a thin dumb adapter.** Logic has one source of truth (Python,
already covered by the suite); the TypeScript shell only renders images it is
handed and forwards presses.

## Scope (v1)

- Live agent status on keys (color + agent-type glyph + needs-you badge).
- **Focus** a selected agent (guarded herdr focus).
- **Approve / Deny** a blocked agent via its answer profile.
- **Stop** (force) a selected agent, with arm-then-confirm.
- **Local and remote** herdr (zero-config loopback bridge, or remote bridge over
  Tailscale) — inherited from the existing herdeck runtime.
- Works on any keypad deck (Mini 6 / MK.2 15 / XL 32); ships recommended default
  layouts.

### Out of scope (v1)

- **Multi-option prompts.** A blocked agent may present a numbered choice
  (`1/2/3`), not a binary approve/deny. v1 does **not** render arbitrary
  multi-option prompts on keys. Approve/Deny use the agent's answer-profile key
  sequences (the "default / first / yes" and "no" answers); for a genuine choice,
  **focus into the TUI/web and answer there** (focus gets you onto that pane). This
  boundary is **enforced**, not just documented: Approve/Deny are disabled when a
  multi-option prompt is detected (see Selection), so Approve never silently
  mis-answers one.
- send-text macros, launching agents, profile switching (later versions).
- Bundling a frozen Python runtime in the plugin (see Process lifecycle —
  follow-up). v1 requires herdeck installed.
- Touchscreen / dials (Stream Deck Plus/Neo) — keypad-only v1.

## Architecture

Three pieces:

1. **Python brain** — the existing herdeck core (bridge/connector, commands +
   answer profiles, model, settings/profiles, `icons.py` rendering, safety policy)
   plus a **new `elgato-plugin` deck driver** and an **Elgato session model**
   (slot leases, selection, arm state). This is the single source of truth.
2. **TS shell** — a thin `@elgato/streamdeck` plugin: declares the action types,
   reports key lifecycle / coordinates / presses to the brain, and renders images
   the brain hands back. No logic, no state of record.
3. **Local IPC socket** between them (reuse herdeck's existing socket machinery).

The brain is just **one more front-end over the existing core**, alongside the
`d200` / `web` / `fake` drivers. New code is the `elgato-plugin` driver + Elgato
session model (Python), the TS shell, and the IPC adapter. Everything else
(herdr I/O, command pipeline, answer profiles, safety, rendering) is reused.

> **Note:** the deck kind is `elgato-plugin`, distinct from the existing `elgato`
> kind, which drives an Elgato deck **directly over HID** (and requires closing the
> Stream Deck app). The plugin path coexists with the Stream Deck app instead.

### Process lifecycle

This is the load-bearing decision for distribution; it is specified here, not
deferred to implementation.

- **What the backend is:** the herdeck app launched with the `elgato-plugin` deck
  driver. It speaks IPC to the TS shell instead of rendering to HID/web.
- **Who owns the process:** the **TS plugin spawns and supervises the backend**.
  When the Stream Deck app starts the plugin, the plugin starts the Python backend;
  when the plugin stops, it terminates the backend. No separate launchd/systemd
  daemon for the end user to bootstrap.
- **Cold start:** on plugin startup the TS shell spawns the backend, then retries
  connecting to the IPC socket until the backend signals ready. While waiting, keys
  render a `starting…` state. If the backend exits unexpectedly, the shell respawns
  it (bounded backoff) and shows a `backend down` state.
- **Discovery + auth:** the **plugin generates the socket path and a one-shot
  token** and passes them to the spawned backend via args/env (the same shape as
  Elgato's own registration handshake passing `-port`/`-pluginUUID`). There is no
  fixed well-known socket; the shell and the backend it spawned share a per-launch
  secret. The backend rejects IPC connections without the token (constant-time
  compare, matching the bridge's auth posture).
- **Finding herdeck (v1):** the plugin spawns `herdeck` from a configured or
  discovered path (Property Inspector setting; fall back to `PATH` / a known venv).
  v1 **requires herdeck installed**. Shipping a frozen single-binary backend
  (PyInstaller) inside the `.sdPlugin` bundle so the plugin is self-contained is a
  **packaging follow-up**, not v1.
- **Dev mode:** if a backend is already running (e.g. a launchd job for local
  development), the shell connects to its known socket instead of spawning — a dev
  convenience, not the distribution path.

## IPC contract

Newline-delimited JSON over the local socket. Versioned so the two sides can update
independently.

**TS → brain:**

- `hello { protocol_version, device, size, token }` — on connect; brain rejects on
  token mismatch or unsupported `protocol_version` (degrade to an error key state
  rather than misbehave on drift).
- `slots [ { instanceId, coord:{col,row} } ]` — every live **Agent Slot** instance.
  The brain derives the ordinal slot index 0..N-1 by sorting coordinates in reading
  order (row, then column).
- `action_keys [ { instanceId, type, coord } ]` — live Approve/Deny/Stop/Pager
  instances.
- `keyDown { instanceId }` / `keyUp { instanceId }` — presses are split so the
  shell can show an **optimistic highlight on keyDown** while the brain acts on
  **keyUp** (and so arm/confirm and any future hold gestures are unambiguous).
- `bye { instanceId }` — on willDisappear.

**Brain → TS:**

- `render { instanceId: { image: <base64 png>, title? } }` — per-instance image,
  pushed on every relevant state change (status / selection / arm / pending).
  **Coalesced and diffed:** only instances whose image actually changed are sent,
  and bursts are throttled, so the shell never spams `setImage` over USB (reuse the
  web driver's per-tile version / only-on-change pattern).
- `ready {}` — backend is up and connected to herdr; ends the `starting…` state.

## Action types, layouts, degradation, reachability

Action types (in a "herdr" category): **Agent Slot, Approve, Deny, Stop, Pager.**

Recommended default layouts (shipped as Elgato profiles per deck):

- **Mini (6):** 2 Agent Slots · Approve · Deny · Stop · **Pager**.
- **MK.2 (15):** ~8 Agent Slots · action row (Approve · Deny · Stop · Pager) · spares.
- **XL (32):** many Agent Slots · action keys · Pager.

The plugin is robust to **any** composition (the user places keys themselves):
1 slot = single-agent mode; a missing action key = that action simply unavailable
from the deck.

**Reachability is a guarantee, not a documented limit.** Whenever there can be more
agents than slots, the recommended layout **must** include a Pager, and the default
profiles always do. Because **selection is global** (see below), the Pager walks
selection through **all** attention agents — including blocked agents that have no
visible slot — and the action keys render the selected target's identity even when
it is off-slot. So every blocked agent is always selectable and actionable
regardless of slot count. Omitting the Pager when agents can exceed slots sacrifices
reachability of overflow agents; the default layouts prevent that.

### Slot model

- **Coordinate-derived ordinal.** The user drops N Agent Slot keys; the brain
  assigns ordinals 0..N-1 from reading-order coordinates. No manual per-key index.
  (Caveat: if the user physically rearranges keys, ordinals remap — a deliberate
  user action, not autonomous movement, so acceptable.)
- **Sticky leasing, never reflow.** An ordinal slot **leases** an agent and keeps
  it until that agent disappears. New agents take the lowest free ordinal. Existing
  agents **never move** between glances. A freed slot becomes a hole, backfilled
  only by a newcomer. This eliminates the physical-surface footgun where the agent
  under a key changes between glances (the reason status must **never** drive
  reordering). Blocked is signalled by **color + badge in place**, not by floating
  the agent up.

## Selection + safety semantics

The heart of the design; all of it lives in the Python brain.

- **Global selection:** at most one selected agent, independent of which slot/page
  shows it; survives paging; lives in the brain.
- **Select:** pressing an Agent Slot selects + focuses that agent (guarded herdr
  focus).
- **Auto-select:** if exactly one agent is blocked and nothing is manually
  selected, auto-select it (common case = one press to act). Precedence: manual >
  auto. Re-evaluated when the target leaves blocked or another agent blocks.
- **Action keys reflect the selected target.** Each Approve/Deny/Stop key renders
  the target's identity (label/repo + agent glyph) and is enabled only when valid:
  - Approve / Deny: enabled iff the selected agent is **blocked**, its server is
    online, **and its prompt has been read and classified binary** (no numbered
    options, via the existing `parse_options`). A multi-option prompt (`1/2/3`)
    leaves Approve/Deny **disabled** with a "→ answer in terminal" hint; an unread
    prompt leaves them pending. So Approve never answers before the prompt is read
    and never silently picks option 1 on a multi-option prompt — it offers only the
    profile's binary answer when the prompt is genuinely binary.
  - Stop: enabled iff a selected agent exists and its server is online (applies to
    working or blocked).
- **Guarded acts (reuse).** Approve/Deny emit `act_if_blocked`, which no-ops on the
  bridge if the agent is no longer blocked — latency-safe, so a stale press can't
  approve the wrong state.
- **Stop = arm-then-confirm (always).** Stop is `act_force` (force stop — sent with
  `guard=false`, so unlike Approve/Deny it is **not** bridge-guarded). It is more
  dangerous, so the Elgato Stop key is **always** two-step, independent of the
  active safety profile: arm-then-confirm is its safety gate. A profile's
  `require_confirm_for` can only make confirmation stricter, never remove it — the
  default `require_confirm_for = []` does **not** weaken Stop here. Expressed as a
  **stateful, timed** key rather than double-click timing:
  1. first press **arms** the key (red, `STOP?` glyph, ~3 s timer);
  2. a press while armed **fires** `act_force`;
  3. timeout / selection change / target leaving → **disarm**.
  The arm is bound to `(action, target)`; any selection change resets it, so you can
  never Stop Y while believing it is armed for X. Deny stays single-press (it only
  refuses a permission); confirm is reserved for Stop (it kills running work).
- **Pending (reuse).** After an act is sent, the action key shows a **pending**
  state until the bridge result / next snapshot confirms the state change (reuse the
  existing `req → handle_result → re-list` flow). Non-idempotent sends are never
  retried (no double-approve), and the pending visual discourages double-press.
- **Pager:** when any agent needs attention, cycle selection through the attention
  agents (blocked, then done); otherwise page the slot grid (when agents exceed
  slots).

## Rendering

Reuse `icons.py`: the brain renders each key as a PNG and hands it to the shell via
`setImage` (base64). Key content:

- **Agent slot:** repo/label + status color + agent-type glyph + needs-you badge.
- **Action key:** action glyph + target identity + state (enabled / disabled /
  armed / pending).

Status colors stay consistent with herdeck (working green / blocked amber + badge /
done / idle / offline red). Elgato keys are ~72–96 px; reuse the existing tile
renderer with adjusted sizing. The brain renders **only on change** and coalesces
bursts (per-instance version compare, like the web driver) to avoid USB spam.
Optimistic highlight on keyDown is the shell's job; the authoritative image always
comes from the brain.

## Error / latency / reconnect

- **Brain ↔ herdr/bridge:** reuse herdeck's reconnect + full resync on reconnect.
  An offline server renders its leased slots as offline and disables action keys for
  its agents.
- **IPC reconnect (backend stays alive):** if the TS↔brain socket blips while the
  backend process keeps running (transient disconnect, or the Stream Deck app
  reconnecting to a live plugin), the brain is the state of record — on reconnect it
  re-pushes a full render and nothing is lost (leases / selection / arm preserved).
- **Full plugin restart (backend killed too):** because the TS plugin owns the
  backend lifetime (see Process lifecycle), stopping the plugin terminates the
  backend, so a full plugin restart **resets** session state (leases / selection /
  arm) — acceptable, since that state is ephemeral. The shell shows `starting…`
  until `ready`. (A separately-managed dev-mode backend that outlives the shell is
  the one case where state survives a full shell restart.)
- **IPC drop:** the shell retries connecting (and respawns the backend if it died),
  showing a disconnected key state meanwhile.
- **Latency** is covered by guarded acts + pending states + no-retry.

## Testing without hardware

No Elgato deck is available, so the design is verified by unit tests + the existing
simulators; on-device end-to-end is deferred until a deck exists.

- **Python brain (where the logic lives → high coverage):** slot leasing (sticky,
  hole backfill, no reflow), coordinate→ordinal sort, global selection +
  auto-select precedence + invalidation, the **arm state machine** (with an
  **injectable clock** for the timeout, reusing the orchestrator's existing
  `clock` seam), guarded-act and pending transitions, render-decision/diff output.
- **IPC contract:** drive the brain with a **fake TS client** (feed
  `hello`/`slots`/`action_keys`/`keyDown`/`keyUp`, assert emitted `render`s and the
  bridge commands produced), reusing the `FakeRenderer`/pipeline-against-fakes
  pattern.
- **TS shell (thin):** unit-test the mapping (SDK events → IPC, `render` → setImage)
  with the Stream Deck SDK test utilities.
- **End-to-end on hardware:** deferred; the web simulator / fake deck stand in for
  visual and interaction sanity of the brain's render and selection decisions.

## Open questions / follow-ups

- **Frozen-binary packaging** (PyInstaller backend inside the `.sdPlugin`) so the
  plugin is self-contained and Marketplace-distributable without a separate herdeck
  install.
- **Multi-option prompts** on the deck (beyond binary approve/deny) — a richer
  future action type, or keep delegating to focus-into-TUI.
- **Raise host terminal on focus** (osascript `activate`) for the local case, behind
  a configured terminal-app name — meaningful only when the deck and herdr share a
  machine.
- **Stream Deck Plus/Neo** touchscreen + dials as a richer surface.
