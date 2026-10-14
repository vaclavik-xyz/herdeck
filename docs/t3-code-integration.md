# T3 Code integration (orchestration protocol 2)

Status: implemented for T3 **0.0.46 and newer** (orchestration protocol 2).
Verified live on 2026-10-14 against `t3@0.0.46-preview.20261010.2909`.
Design: [the V2 spec](superpowers/specs/2026-10-14-t3-orchestration-v2-design.md).
Upstream audit: [t3-orchestration-v2-2026-10-14](research/t3-orchestration-v2-2026-10-14.md).
Setup and operations: [agent-setup.md](agent-setup.md#t3-code-setup).

Herdeck shows T3 conversations next to Herdr agents and routes every action to
the backend that owns the thread. Herdr workflows are unaffected when T3 is
absent or offline.

## Requirement

T3 0.0.46+ speaks orchestration protocol 2. Until it reaches the stable
channel, install the preview/nightly build (`npm i -g t3@preview`) or switch
the T3 desktop app to its preview channel. Stable 0.0.45 and older (protocol 1)
are not supported: the connection stays offline with
`T3 server speaks orchestration protocol 1; Herdeck needs 2 (T3 0.0.46+)`.
Herdeck checks the server descriptor first and again after every reconnect.

## Contract summary

All reads are plain HTTP polling (every second); the WebSocket is used only to
dispatch commands. A credential is a bearer token (no DPoP needed).

| Step | Endpoint | Notes |
|---|---|---|
| Descriptor | `GET /.well-known/t3/environment` (no auth) | `orchestrationProtocolVersion` must be exactly 2 (absent means 1); `capabilities` advertise `threadSettlement`, `threadSnooze`, `threadVisitedTracking`, `serverResolvedCommandContext` |
| Shell | `GET /api/orchestration/shell` | `threads`, `archivedThreads`, `projects`; needs `orchestration:read` |
| Detail | `GET /api/orchestration/threads/:id/bounded` | prompt of a pending request, plan text, last assistant message; the unbounded `/threads/:id` is used only when the bounded plan text is left in a turn item |
| Ticket | `POST /api/auth/websocket-ticket` | bearer, no body; the ticket lasted 5 minutes on the server we observed |
| Dispatch | `ws(s)://host/ws?orchestrationProtocol=2&wsTicket=...` | one `orchestration.dispatchCommand` request, wait for the `Exit`, close; `Ping` is answered with `Pong` |

Facts checked on the live server: shell and detail reads without the
`x-t3-orchestration-protocol: 2` header fail with HTTP 400; the WebSocket
without `orchestrationProtocol=2` fails with HTTP 426; a dispatch result is
`{sequence}`; replaying a `commandId` returns the same result without
executing it again; a schema error comes back as `Die`, a refused command as
`Fail` (`OrchestrationV2DispatchCommandError`); `thread.visit` sets the shared
`lastVisitedAt`. HTTP errors: 401 invalid or expired credential, 403 missing
scope (`insufficient_scope` as observed), 404 unknown thread.

Credential scopes: `orchestration:read` (tiles) and `orchestration:operate`
(actions). Setup and renewal issue both:

```sh
t3 auth session issue --scope orchestration:read --scope orchestration:operate \
  --ttl 30d --label "Herdeck <id>" --json
```

Scopes are not checked at connect. With a read-only token the tiles stay live
and show normal controls until the first action is refused for the missing
scope; from then on actions are disabled for that connection and every tile
says `T3 credential lacks orchestration:operate — actions disabled.`

## Status mapping

First matching row wins, lifecycle first (`src/herdeck/t3_state.py`).

| Condition | Status | Label / attention |
|---|---|---|
| deleted, archived, settled, or snoozed (`snoozedUntil` in the future, no raised hand) | IDLE | DELETED / ARCHIVED / SETTLED / SNOOZED; not counted as active work |
| pending request, kind other than `user_input` / `auth_refresh` | BLOCKED | approval |
| pending request `user_input` / `auth_refresh` | BLOCKED | input |
| `preparing` / `starting` | WORKING | CONNECTING |
| `queued`, or a message sent in the last 2 minutes that no run has picked up | WORKING | QUEUED |
| `running` | WORKING | none |
| `waiting`, or `completed` with a pending background task other than `command` | WAITING | SUBAGENT / COMMAND / MONITOR / BACKGROUND (first holding task) |
| `failed` | UNKNOWN | ERROR, or LIMIT for `usage_limit`; preview `T3 error: ...` |
| plan mode, an actionable proposed plan and no active run (settled) | BLOCKED | PLAN READY |
| `completed` and `latestRunCompletedAt` newer than `lastVisitedAt` | DONE | completion |
| anything else (idle, completed and seen, never visited, interrupted, cancelled, rolled back) | IDLE | none |

A snoozed thread wakes on its own when the snooze ends or when a "raised hand"
appears (a pending request, a new failure, a completion after the snooze, a
queued turn). Archived threads stay in the snapshot so a deck pin keeps its
tile, but do not count as active work.

## Done and seen

Done is decided by the T3 server: a completed run is Done while its completion
is newer than the thread's `lastVisitedAt`. That timestamp is shared by every
client of the server. Opening the thread in T3 desktop or mobile clears Done on
the deck as well, and the deck's **Mark seen** sends `thread.visit`, which
clears it everywhere.

A completed thread that was never opened is not shown as Done, the same as in
T3 itself.

## Actions and commands

Offered only for threads with an active lifecycle (settle and wake-up controls
excepted). Commands are built in `src/herdeck/t3_actions.py`.

| Deck action | When offered | Command |
|---|---|---|
| Approve / Approve for session / Approve always / Deny | approval request that is still live; one action per option T3 lists (session and always need a second press) | `runtime-request.respond` with `decision` |
| Answer | user-input request that is live and has a single question without multi-select; one action per option | `runtime-request.respond` with `answers` |
| Stop | a run is active | `run.interrupt` (`holdQueue: true`) |
| Reply | status IDLE or DONE | `message.dispatch`, 1 to 4096 characters |
| Implement plan (confirm) | PLAN READY with an active plan | `thread.interaction-mode.set` to `default`, then `message.dispatch` with `PLEASE IMPLEMENT THIS PLAN:` and the plan (up to 120000 characters) |
| Mark seen | DONE and `threadVisitedTracking` | `thread.visit` with the completion time |
| Settle / Reopen | `threadSettlement`; Settle on idle, done or failed threads | `thread.settle` / `thread.unsettle` |
| Snooze 1h / Wake now | `threadSnooze`; not while awaiting approval or input, or while a start is queued | `thread.snooze` / `thread.unsnooze` |

A reply sent while a run is active and the server does not resolve context
itself is queued after the run (`queue_after_active`).

## Delivery rules

Every press re-reads the thread and must carry the revision the deck showed
and an action that is still offered, otherwise the connector answers "Stale or
already submitted T3 action". Each command gets a `commandId` that is
remembered per thread, revision, action, payload and reply text.

- Transport failure: the same command is retried once after one second.
- Still failing: the result is uncertain. The tile stays read-only until the
  thread changes, because the command either landed (the thread changes) or
  did not. The deck offers no buttons on the read-only tile; a client that
  still holds the older revision (for example herdeck-ctl) can send the
  identical press (same revision, action, payload), which resends the same
  `commandId`, which T3 de-duplicates, so a command cannot run twice. A
  refused WebSocket upgrade on the retry also counts as uncertain.
- Rejected by the server (`Fail`, `Die`, missing scope): the message is
  reported, the revision is not consumed and the stored ids are dropped.
- Accepted: the thread is refreshed immediately. Implement plan is two
  commands in order and stops at the first failure.
- A credential without `orchestration:operate` disables actions for the whole
  connection until Herdeck is restarted with a new token.
- One malformed thread degrades to a read-only tile and never fails the others.
- Detail read failures keep the cached detail and mark the tile `STALE`
  (read-only). A 404 drops the thread; 401 or 403 take the connection offline.

## Known limits

- A never-opened completed thread is not Done (same as T3).
- Approval answers are offered only for live requests, and questions only when
  there is one single-choice question. Everything else says "Answer in T3".
- After an uncertain delivery the tile stays read-only until the thread
  changes; the deck offers no buttons meanwhile, and only a client holding the
  older revision (for example herdeck-ctl) can resend the same command.
- The deck reports a T3 rejected or uncertain result as "sent". The result is
  in the connector (`rejected` / `uncertain` with a message) but the deck does
  not show it yet. This is tracked as a follow-up.
- No raw terminal keys, streaming or desktop thread navigation.

## What was removed, and why

- **V1 support** (T3 0.0.38 and older, protocol 1). The V1 shell has no true
  per-thread status and no shared read state. A V1 server now shows as
  incompatible.
- **The local read-state bridge** (`t3_seen.py`, `t3_desktop_seen.py`, the
  LevelDB reader of the T3 desktop UI state). The server's `lastVisitedAt`
  replaces it for every client. `[[servers]] desktop_read_state` and
  `HERDECK_T3_DESKTOP_READ_STATE` are still accepted, ignored, and logged once
  as obsolete; the editor toggle is gone.
- **Stop session** (V1 background-session control). Stop is `run.interrupt`.

## Topologies

- A T3 desktop app or server on the same Mac: setup discovers it from
  `<base-dir>/userdata/server-runtime.json`.
- A remote headless server through an SSH tunnel (macBench keeps it with the
  `com.herdeck.t3-forward` LaunchAgent, `ssh -N -L
  127.0.0.1:13773:127.0.0.1:3773 <user>@<host>`, server URL
  `http://127.0.0.1:13773`). Renewal then uses `--issuer-ssh`.

See [agent-setup.md](agent-setup.md#dedicated-t3-transport-and-renewal-macos).
