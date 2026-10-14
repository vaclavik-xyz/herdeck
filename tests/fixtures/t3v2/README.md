# T3 orchestration V2 fixtures

Captured 2026-10-14 from a live `t3@0.0.46-preview.20261010.2909` server
(isolated base dir, loopback) driven over the WebSocket dispatch RPC, provider
Claude (`claudeAgent`, `claude-haiku-5-5`). One thread per file (`threads[:1]`),
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
| `shell_input.json`, `detail_input.json` | built from the contract the same way — pending `user_input` request with one single-choice question |

Contract reference: `docs/t3-code-integration.md`.
