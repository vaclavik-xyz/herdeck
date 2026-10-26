# T3 Code orchestration V2 — upstream audit, 2026-10-14

Upstream inspected at `pingdotgg/t3code` `7ac952c` (2026-10-14), latest tag
`v0.0.46-nightly.20261014.3105`. Read-only: files, git history and GitHub
PRs/issues; nothing from upstream was executed and no live V2 server was
probed, so the compatibility conclusions below are derived from the contract
and server code, not observed.

Herdeck's T3 backend (`src/herdeck/t3*.py`) was built against 0.0.38; see
[t3-code-integration.md](../t3-code-integration.md) and the earlier research in
this directory.

## The break

Orchestration V2 landed in one PR, #2829 (`de3439142`, merged 2026-10-02):
it deleted `apps/server/src/orchestration/` and
`packages/contracts/src/orchestration.ts` and replaced them with
`orchestration-v2/` and `packages/contracts/src/orchestrationV2.ts`. A thread
now holds runs (one per user-visible turn), each a tree of work items
(`docs/orchestration-v2/README.md`). Existing data migrates on first launch
(`state.sqlite` → `statev2.sqlite`).

- In every tag from `v0.0.46-nightly.20261003` and `v0.0.46-preview.20261008`
  on; not in stable `v0.0.44`/`v0.0.45`. No feature flag.
- Up to 0.0.45 the contract Herdeck uses is unchanged from 0.0.38.
- From 0.0.46 Herdeck cannot connect at all (not even read-only):
  - shell/detail requests must send `x-t3-orchestration-protocol: 2`
    (`environmentHttp.ts:80-84`, `environment.ts:13-16`);
  - `POST /api/orchestration/dispatch` is gone; commands go only over the
    WebSocket RPC `orchestration.dispatchCommand` (`rpc.ts:1620`), opened with
    a ticket from `POST /api/auth/websocket-ticket` and
    `?orchestrationProtocol=2` (else HTTP 426, `ws.ts:3181`);
  - our version regex rejects `0.0.46-nightly.…` server versions.
- Commands were renamed: `message.dispatch` (with `sourcePlanRef`),
  `runtime-request.respond {requestId, decision|answers}`, `run.interrupt`,
  `thread.stop`; settle/unsettle/snooze/unsnooze kept.
- No stable third-party API is promised; the only versioning is
  `ORCHESTRATION_PROTOCOL_VERSION`, and the design docs say they ignore
  backward compatibility on purpose.

## What V2 fixes for us

- **Authoritative thread status.** `OrchestrationV2ThreadShell`
  (`orchestrationV2.ts:1933-2024`) carries `status` (`idle` or the latest
  run's `preparing|queued|starting|running|waiting|completed|interrupted|
  failed|cancelled|rolled_back`), `activityRunStatus`,
  `pendingRuntimeRequest {id, kind, createdAt}` (`kind`: `command|file-read|
  file-change|mcp-elicitation|permission|dynamic_tool_call|user_input|
  auth_refresh`), `lastError`, `lastErrorClass`, `latestRunCompletedAt`,
  `pendingBackgroundTasks`, `hasActionableProposedPlan`, `settledAt`,
  `snoozedUntil`, `latestVisibleMessage`. `RuntimeRequest.status` is
  `pending|resolved|expired|cancelled` (`:1114`).
- **Server-side shared read state.** `lastVisitedAt` on the thread shell,
  set by `thread.visit {threadId, visitedAt}` (server keeps the max,
  `Orchestrator.ts:2346-2360`) and cleared by `thread.mark-unread`.
  "Done, unseen" = `latestRunCompletedAt > lastVisitedAt`. The web UI treats it
  as authoritative and migrates old local markers once
  (`useThreadVisitedMigration.ts`). Mobile issue #4952 was closed by #2829.
- **Idempotent commands.** Dispatch deduplicates by `commandId`
  (`CommandReceiptStore`, `Orchestrator.ts:11035-11075`): replaying the same id
  is safe and returns the original outcome.
- **Streams.** `orchestration.subscribeShell` (snapshot, `thread.updated`,
  `thread.removed`, resumable `afterSequence`) and `subscribeThread`.
- **Auth.** `t3 auth session issue` remains; bearer still accepted; tokens can
  be scoped to `orchestration:read` + `orchestration:operate`.

Remaining upstream gaps: a never-opened thread counts as visited (PR #15607
open); mobile lacks mark-unread (PR #17914); stale projection on flaky
connections (#5742).

## Herdeck workarounds, now

| Workaround | On V2 |
|---|---|
| Derive status from session/latestTurn/activities (`t3.py`, `t3_state.py`) | Replace with the shell's `status` + `pendingRuntimeRequest` (fields renamed) |
| Rebuild pending requests from activity log, "stale pending" heuristics | Unnecessary: `RuntimeRequest.status` |
| Local Done acknowledgment (`t3_seen.py`) | Replace with `lastVisitedAt` / `thread.visit` (shared across devices) |
| Reading the T3 desktop's Chromium leveldb (`t3_desktop_seen.py`) | Unnecessary on V2 |
| Version-regex capability negotiation (`t3_actions.negotiated_features`) | Breaks on nightlies; negotiate `orchestrationProtocolVersion` |
| HTTP dispatch, never replay uncertain writes | Dispatch over WebSocket; replay the same `commandId` |
| Polling + per-thread detail + 15 s cache | Shell carries most of it; or `subscribeShell` |
| SSH credential renewal (`t3_renew`) | Broken since 0.0.42 (runtime moved to `runtime/versions/<v>/t3`) |
| Settle/snooze lifecycle rules | Still needed, renamed fields |
| Discovery via `userdata/server-runtime.json` | Unchanged |

## Ideas for Herdeck beyond T3

- A shared per-episode "visited" watermark in the bridge (like `lastVisitedAt`)
  would clear Done once for deck, dock and Telegram.
- One pure attention function used by every surface (T3:
  `packages/shared/src/agentAwareness.ts`) instead of logic split across
  `protocol.effective_status`, `notify_events.py` and `t3.py`.
- "Completed but held by background work" (`backgroundWorkHoldsCompletion`)
  matches Herdeck's WAITING status and subagent rules.
- Separate transport health from data freshness on reconnect
  (`docs/internals/connection-runtime.md`).

T3 drives agents through SDKs (Claude Agent SDK, Codex app-server), so its
provider adapters do not transfer to Herdeck, which observes Herdr panes.
License: T3 is MIT, like Herdeck.
