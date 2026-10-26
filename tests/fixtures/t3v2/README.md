# T3 orchestration V2 fixtures

Captured 2026-10-14 from a live `t3@0.0.46-preview.20261010.2909` server
(isolated base dir, loopback) driven over the WebSocket dispatch RPC, provider
Claude (`claudeAgent`, `claude-haiku-5-5`). One thread per file (`threads[:1]`;
the `*_subagent*` shells hold a whole family),
detail = `GET /api/orchestration/threads/:id/bounded`, turn items trimmed to the
last 12, local paths replaced by `/Users/dev/...`.

| File | Source |
|---|---|
| `descriptor.json` | live (`/.well-known/t3/environment`) |
| `shell_running.json`, `detail_running.json` | live, run in progress (`status: running`) |
| `shell_completed.json`, `detail_completed.json` | live, run completed; `lastVisitedAt` older than `latestRunCompletedAt` (unseen) |
| `shell_failed.json`, `detail_failed.json` | live, provider 401 (`status: failed`, `lastErrorClass: provider_error`) |
| `shell_plan.json`, `detail_plan.json` | live, plan mode right after the plan was proposed: `status: waiting`, `hasActionableProposedPlan: true`, active `plans[]` entry |
| `shell_approval.json`, `detail_approval.json` | built from the contract on top of `*_completed` (a live approval could not be provoked: the local Claude permission settings allowed the command) — pending `command` request with a live `runtimeRequests[]` entry and an `approval_request` turn item |
| `shell_approval_live.json`, `detail_approval_live.json` | live, Claude provider, real `file-change` approval request (approved afterwards): `options: null`, prompt = the file path (`NOTES.md`), live `runtimeRequests[]` entry; maps to the default Approve/Deny |
| `shell_input.json`, `detail_input.json` | built from the contract the same way — pending `user_input` request with one single-choice question |
| `shell_subagent.json`, `detail_subagent.json` | built from the contract (T3 7ac952c, `OrchestrationV2ThreadShell.lineage`, `OrchestrationV2SubagentJson`) on top of `*_running`: several threads — the running parent, two provider-native Claude subagent children (`relationshipToParent: subagent`, `creationSource: provider`, no runs so `status: idle`, as in upstream's `subagent` scenario), a nested grandchild and a fork; the parent's bounded detail lists four `subagents[]` (running, completed, failed, pending without `startedAt`) |
| `shell_subagent_request.json`, `detail_subagent_request_parent.json`, `detail_subagent_request.json` | built the same way on top of `*_approval`: a completed parent held by a `subagent` background task, and its delegate_task child (`creationSource: mcp`, own run) holding the pending `command` request; the parent's detail lists the child as an `app_owned` `waiting` subagent |

Contract reference: `docs/t3-code-integration.md`.
