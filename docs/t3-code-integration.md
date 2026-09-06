# T3 Code integration: design and implementation plan

Status: T3-01 through T3-05 implemented and verified. T3-06 software verification
passed; the physical deck pilot awaits the user's choice of T3 host.
Updated: 2026-09-06.

## Goal

Use T3 Code for conversations and code review while retaining Herdeck's hardware
interface. Show Herdr agents and T3 conversations together, with correct routing
of every action to its owning backend. Existing Herdr workflows must continue
to work when T3 is absent or disconnected.

The integration is implemented on `feat/t3-code-integration`; it is not released.
Implementation of T3-01 through T3-06 was subsequently authorized. Push, merge,
deployment and infrastructure changes remain outside this work.

## Evidence and compatibility baseline

Local Herdeck checkout inspected at
`d21429ba67b31905836a2f43780a68a46a2c7378`:

- [`model.py`](../src/herdeck/model.py): `AgentKey(server_id, pane_id)` scopes
  identity; `AgentState` carries display state and terminal identity.
- [`deckapp/live.py`](../src/herdeck/deckapp/live.py): `LiveSource` combines
  multiple connections and routes commands; blocked details currently use
  terminal prereads and detection.
- [`commands.py`](../src/herdeck/commands.py): high-level approve/deny/stop
  actions become terminal key sequences. These cannot be reused as T3 actions.
- [`bridge.py`](../src/herdeck/bridge.py): the bridge exposes Herdr-specific
  snapshots, terminal reads, focus and input.
- [`deckapp/source.py`](../src/herdeck/deckapp/source.py): `StateSource` separates
  state ingestion from deck rendering, but is not yet a general backend API.

T3 upstream inspected at `7544d3d2c8e0145018d9adb7a1a650333b75362a`:

- [RPC contract](https://github.com/pingdotgg/t3code/blob/7544d3d2c8e0145018d9adb7a1a650333b75362a/packages/contracts/src/rpc.ts)
  exposes orchestration subscriptions and command dispatch.
- [Orchestration contract](https://github.com/pingdotgg/t3code/blob/7544d3d2c8e0145018d9adb7a1a650333b75362a/packages/contracts/src/orchestration.ts)
  includes `orchestration.subscribeShell`, `orchestration.subscribeThread`,
  `thread.turn.start`, `thread.turn.interrupt`, `thread.approval.respond`, and
  `thread.user-input.respond`.

Live spike: T3 npm package 0.0.31, isolated data directory, loopback port 13773.
Issued a temporary bearer session with `t3 auth session issue` into process
memory and fetched `/api/orchestration/shell` successfully (one project and
one conversation). Existing `~/.t3` data was not modified.

Transport decision: use the authenticated HTTP shell/detail/dispatch endpoints
already exposed by T3, with one-second polling and a fresh pre-action read.
Python's standard library handles the transport; no Node helper is bundled.
Redirects and environment proxies are disabled to avoid credential forwarding.
This replaces the proposed WebSocket transport. Full reconnect and action pilot
evidence will be recorded below; no stable third-party API promise is assumed.
Desktop thread navigation remains deferred because it has not been verified.

## Architecture

Keep the renderer and hardware output shared. Add a T3 adapter alongside the
Herdr path, with backend-neutral state and semantic actions at their boundary:

```text
Herdr connections -> Herdr adapter --+
                                    +-> shared agent state -> Herdeck -> deck
T3 server --------> T3 adapter ------+

deck action -> owning adapter -> owning backend
```

Introduce this boundary incrementally around the existing live source and
command path. Do not implement T3 by pretending it is a terminal or by starting
another Codex process for the same conversation. Prefer consuming T3's existing
backend without a T3 fork; revisit only if T3-01 proves that insufficient.

The adapter uses Python HTTP polling. Thread reads request the latest three turns
to bound history transfer. The packaged runtime has no Node helper dependency.

### Identity, state and capabilities

- Identify T3 conversations by backend instance plus thread ID. Provider kind
  (for example Codex) is separate from backend kind (T3 versus Herdr).
- Preserve Herdr wire compatibility. Do not use a fabricated terminal ID to
  bypass existing identity checks; define explicit backend identity instead.
- Map active turns to `WORKING`, pending approvals or questions to `BLOCKED`,
  and inactivity to `IDLE`. Specify completion/error mappings against observed
  T3 events before using `DONE`; inactivity alone does not prove task completion.
- Disconnection means unavailable with actions disabled, not successful
  completion. Unknown upstream states remain `UNKNOWN`.
- Preserve project, branch and title when supplied. Missing fields remain empty.
- Capabilities describe available actions and preview type. T3 previews show
  the latest message or pending request, not an emulated terminal screen.
- Handle structured questions separately from approval prompts; arbitrary
  questions must not be reduced to Approve/Deny buttons.

### Action mapping

| Herdeck intent | T3 operation | Required guard |
| --- | --- | --- |
| Stop | `thread.turn.interrupt` | Current thread and active turn |
| Approve / Deny | `thread.approval.respond` | Exact pending request and supported decision |
| Answer question | `thread.user-input.respond` | Exact request and validated answer schema |
| Continue / text macro | `thread.turn.start` | Idle thread, explicit text, preserved mode/model settings |
| Open conversation | Deferred | No supported desktop navigation verified |

Do not infer decisions from terminal key profiles. Do not silently implement
“approve always” or force actions where T3 lacks a verified equivalent. Continue
must preserve the conversation's runtime and interaction mode rather than
accidentally selecting permissive defaults.

Recheck request/turn identity at dispatch. Invalidate pending actions on request
resolution, deletion and reconnect. A concurrent response in T3 must make the
old deck action stale. Do not automatically replay writes after a disconnect or
timeout; reconcile backend state before reporting success or allowing a retry.

## Delivery plan

IDs are stable for later issue and PR references. The tracking table below
records implementation and live verification separately from the planned scope.

| ID | Deliverable | Depends on | Acceptance evidence |
| --- | --- | --- | --- |
| T3-01 | Compatibility spike and transport decision | — | Connect to one actual T3 instance, obtain snapshot and updates, verify auth/discovery and reconnect; record version, framing, navigation and credential handling without secrets |
| T3-02 | Minimal backend boundary and capabilities | T3-01 | Existing Herdr behavior passes; synthetic T3 state renders beside Herdr; colliding thread/pane IDs route independently; unavailable actions are disabled |
| T3-03 | Live T3 tiles and message/request preview | T3-02 | Create a conversation in T3, run a turn and observe state/title/project on the deck; restart connection without duplicates or false completion |
| T3-04 | Stop and explicit Continue/text actions | T3-03 | Stop affects only the selected active turn; Continue sends once to the selected idle conversation and preserves settings; uncertain delivery is surfaced |
| T3-05 | Approval and structured question handling | T3-04 | Resolve a real pending approval and supported question from the deck; reject stale requests already handled in T3; unsupported choices remain unavailable |
| T3-06 | Connection setup, packaging and end-to-end pilot | T3-05 | Packaged runtime connects without user-managed dependencies; mixed Herdr/T3 pilot passes hardware and desktop checks; setup and removal documented |

First usable milestone: T3-01 through T3-03, with live tiles and previews.
Opening a conversation is included only if T3-01 verifies a supported mechanism;
otherwise record it as deferred rather than blocking state display.

Full initial integration: T3-01 through T3-06. Approval support must be tested
with a T3 mode that actually requests approvals. Test fixtures alone are not
proof of interoperability.

### Expected implementation areas

- New adapter module(s) and transport tests; final filenames follow T3-01.
- `model.py`, `commands.py`, `orchestrator.py`: identity, capabilities, semantic
  action routing and presentation of structured requests.
- `deckapp/live.py`, connector/configuration paths: mixed sources, lifecycle,
  reconnect and per-backend health.
- Desktop setup and packaging: T3 connection discovery/configuration and any
  required bundled transport. Review other clients of shared contracts,
  including the semantic API, browser dashboard and Elgato plugin.
- `docs/agent-setup.md`: add verified T3 setup/removal instructions at T3-06.

## Verification and rollout

Use contract fixtures from the pinned T3 revision for state mapping, event
ordering, reconnect/resync, capability handling and stale request rejection.
Cover duplicate presses, uncertain write outcomes, a thread removed mid-action,
and two backend instances with the same thread ID. Run relevant existing Herdr
tests whenever shared contracts change.

Pilot on one project with both backends connected. Verify display, disconnect,
Stop, Continue, approval and question handling on the hardware deck and desktop.
Capture versions and non-secret results in this document or linked test report.
Completion requires observed backend effects, not only a successful button press.

Keep T3 opt-in. Removing/disabling its connection restores Herdr-only operation
without deleting T3 conversations or modifying provider credentials. Before
runtime configuration changes follow `agent-setup.md`, inspect actual topology
and back up relevant configuration. Do not log or commit secrets. No new DNS,
public listener, Serve/Funnel, tunnel, proxy or firewall rule is authorized by
this plan. Remote T3 discovery/authentication is a later scope unless the pilot
requires it and its topology is explicitly agreed.

For implementation commits follow AGENTS.md: Conventional Commits and inspect
the automatic Roborev result for each SHA, with bounded review/fix rounds.
Push, merge and deployment require separate authorization.

## Deferred scope

- Creating projects, worktrees or new conversations from the deck.
- Migrating or sharing existing Herdr conversations with T3.
- Full chat/diff rendering, T3 terminal emulation and global shortcuts.
- Provider-account management or guessed provider usage attribution.
- Remote T3 setup, an upstream T3 fork, and broad backend framework refactoring.

## Tracking

Update each entry with its issue/PR, exact tested revisions and remaining limits.
Do not mark milestones complete based solely on implementation or mocked tests.

| ID | Status | Issue / PR / evidence |
| --- | --- | --- |
| T3-01 | Verified | T3 0.0.31 live HTTP/auth/discovery and real restart probe; Python transport |
| T3-02 | Verified | `2978baf`; mixed-backend routing, identity collision, disabled offline actions in `tests/test_t3.py` |
| T3-03 | Verified | Live conversation, assistant reply and working/idle states; packaged mixed connections |
| T3-04 | Verified | Live Continue and Stop; mode preservation and duplicate/stale-write tests |
| T3-05 | Verified | Real Codex approval and single-choice question resolved via T3; `0e8ef3e` reconciles uncertain writes |
| T3-06 | Software verified; hardware pending | `dcf4ae1`, `ea56a5e`; setup/keychain/doctor, clean install, desktop build, frozen mixed-runtime probe; physical host choice pending |

## Verification record (2026-09-06)

- Python suite: 1,505 tests passed before the final probe-cleanup change; two
  additional credential-cleanup tests passed afterwards. The cleanup change does
  not alter the runtime adapter.
- Desktop: 168 focused configuration/help tests passed; production frontend
  build passed. Editing connections preserves the T3 backend field.
- Clean base-package installation under Python 3.13.13 successfully executes
  `herdeck-t3-connect --help` without extras. Base dependencies now include
  keyring and tomli-w.
- Real T3 probes: assistant response `HERDECK_PILOT_OK`, working-to-idle state,
  targeted Stop, approval resolution, and single-choice question resolution.
- Real restart probe: same conversation IDs after server restart, offline state
  observed and pre-reconnect action revisions invalidated.
- Frozen macOS arm64 runtime: `source=live`, `connections={local: true,
  t3-pilot: true}`. It starts without user-installed Node. The probe does not
  write the shared runtime discovery file.
- Setup tested against an isolated T3 data directory and dedicated test keychain
  namespace; doctor passed. No installed Herdeck configuration was modified.

Reproducible opt-in probes live in `tests/t3_live_probe.py` (reply/stop/approval/
question scenarios), `tests/t3_reconnect_probe.py`, and
`tests/t3_packaged_probe.py`. Run only against disposable T3 data as documented
in their module docstrings. Probe sessions are revoked in `finally`.

Review record: initial automatic jobs failed to initialize the configured ACP
reviewer. Verified infrastructure-only retries used Codex with an available
model. Review `23728` found one Medium (uncertain writes never unlock), fixed in
`0e8ef3e`; verification `23730` found no issues. Review `23732` found two Mediums
(base CLI dependencies and probe token cleanup), fixed in `ea56a5e`;
verification `23734` found no issues.

One Low is recorded without expanding the review-fix scope: doctor currently
checks a T3 shell's `threads` array while the connector also requires `projects`.
An incompatible server may therefore pass doctor but fail runtime connection;
the mandatory runtime health check remains necessary.

The branch exceeds the approximate 1,000-line review guideline because it
includes transport, its shared action boundary, setup, tests and documentation
for one feature. It remains together so a checkout is runnable; logical changes
are separated into commits and reviewed individually. No repeated whole-branch
review was requested or run.

Physical pilot is not complete: Ulanzi is present on macBench, with the installed
Herdeck desktop and runtime active. The test T3 instance is on HEADLESS-A3112.
The user has been asked whether T3 should run on macBench or HEADLESS-A3112;
the existing macBench runtime has not been interrupted, replaced or reconfigured.
Remote T3 networking is not silently introduced to bridge this choice.

### Physical follow-up (2026-09-06)

The real T3 0.0.38 server now supplies macBench alongside Herdr. The initial
optimized USB writer left the physical deck showing an old page even though
runtime health and HID packet byte counts looked successful. The user confirmed
that the physical display worked after selecting the standard strmdck writer in
a source runtime. This confirms display delivery only; physical approval, Stop,
and question handling still need separate acceptance. See the compatibility and
connection-lifecycle notes in `agent-setup.md`.

The follow-up tile correction uses T3's editable project name and thread title,
updates both on subsequent snapshots, and labels backends `T3` / `HERDR` instead
of abbreviating internal connection IDs. Focused tests cover snapshot renames,
explicit secondary layouts, missing project-title fallback, and the standard
writer override.
