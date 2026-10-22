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
dispatch commands and to ask for project favicon URLs. A credential is a bearer
token (no DPoP needed).

| Step | Endpoint | Notes |
|---|---|---|
| Descriptor | `GET /.well-known/t3/environment` (no auth) | `orchestrationProtocolVersion` must be exactly 2 (absent means 1); `capabilities` advertise `threadSettlement`, `threadSnooze`, `threadVisitedTracking`, `serverResolvedCommandContext` |
| Shell | `GET /api/orchestration/shell` | `threads`, `archivedThreads`, `projects`; needs `orchestration:read` |
| Detail | `GET /api/orchestration/threads/:id/bounded` | prompt of a pending request, plan text, last assistant message; the unbounded `/threads/:id` is used only when the bounded plan text is left in a turn item |
| Ticket | `POST /api/auth/websocket-ticket` | bearer, no body; the ticket lasted 5 minutes on the server we observed |
| Dispatch | `ws(s)://host/ws?orchestrationProtocol=2&wsTicket=...` | one `orchestration.dispatchCommand` request, wait for the `Exit`, close; `Ping` is answered with `Pong` |
| Favicon | same WebSocket, `assets.createUrl` with `{resource: {_tag: "project-favicon", cwd: workspaceRoot, path?: faviconPath}}`, then `GET relativeUrl` | needs only `orchestration:read`; the signed `/api/assets/...` URL lasts an hour; 404 = no favicon |

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

## Subagents and lineage

A thread whose `lineage.relationshipToParent` is `"subagent"` is a subagent
child: a provider-native subagent (Claude's Agent tool, Codex or OpenCode
subagents; `creationSource: "provider"`) or a T3 `delegate_task` child
(`creationSource: "mcp"`). T3 hides these in its sidebar, its notifications
and its mobile list, and Herdeck does the same: a child is not a tile, its
detail is not read, and it raises no notification. Forks
(`relationshipToParent: "fork"`) stay tiles of their own.

Requests. A provider-native subagent's approval or question is asked on the
parent's thread and run: the parent shell's `pendingRuntimeRequest` carries
it, the child shell's stays `null`, and the parent's tile answers it with
`runtime-request.respond` on the parent thread (upstream fixtures
`subagent_v2_approval` and `opencode_running_child_approval`). A delegate_task
child runs its own provider session, so a request it waits on lives only on
the child's own shell (the shell's request is never rolled up from other
threads). So the rule is: **a subagent child that holds a pending request,
and is neither archived nor deleted, is shown as its own tile until the
request is answered**, with the usual approve and answer actions on the
child's thread. Then it is hidden again.

The parent's subagent list comes from the `subagents` array of the parent's
detail. That array is in the `/bounded` detail too (all active subagents plus
those of its recent runs), and a `subagent.updated` event bumps the parent's
`updatedAt`, so the parent's detail is read again when one changes. Child
shells cannot be used for this: a provider-native child has no runs, so its
shell status stays `idle` while it works. Each projection lists only its own
direct subagents, so the list has depth 1 only.

| T3 subagent status | Herdeck subagent status |
|---|---|
| `pending`, `running`, `waiting` | running |
| `idle`, `completed` | done |
| `failed`, `cancelled`, `interrupted` | failed |
| anything else | dropped |

Fields: `title` is the label (`type`), the first prompt line is the
description, plus `driver`, `model` (or `modelSelection.model`), `startedAt`
(`updatedAt` for a task that has not started yet) and `completedAt`. Ids longer
than the model bound are hashed rather than clipped. The text and count bounds
are the shared ones (`parse_subagents`: at most 20, most recent first);
`subagents_running` and `subagents_total` count every listed subagent. The
list is not part of the tile's revision, because no deck action depends on it.

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
| Reply | status IDLE or DONE, or PLAN READY | `message.dispatch`, 1 to 4096 characters; on PLAN READY the thread stays in plan mode and the reply refines the plan (no `sourcePlanRef`), as T3's own plan follow-up does |
| Implement plan (confirm) | PLAN READY with an active plan | `thread.interaction-mode.set` to `default`, then `message.dispatch` with `PLEASE IMPLEMENT THIS PLAN:` and the plan (up to 120000 characters) |
| Mark seen | DONE and `threadVisitedTracking` | `thread.visit` with the completion time |
| Settle / Reopen | `threadSettlement`; Settle on idle, done or failed threads | `thread.settle` / `thread.unsettle` |
| Snooze 1h / Wake now | `threadSnooze`; not while awaiting approval or input, or while a start is queued | `thread.snooze` / `thread.unsnooze` |

A reply sent while a run is active and the server does not resolve context
itself is queued after the run (`queue_after_active`).

## Delivery rules

Every press re-reads the thread and must carry an action that is still
offered ("T3 action is not available" otherwise, checked first) and the
revision the deck showed ("Stale or already submitted T3 action" otherwise).
Each command gets a `commandId` that is remembered per thread, revision,
action, payload and reply text.

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

The texts Herdeck writes into a tile preview itself (the notices above, "Answer
in T3", "T3 error: ...", "Plan ready", the lifecycle prefix, the untitled
thread name) follow `[view].language` (`src/herdeck/i18n.py`, keys `t3.*`); a
config reload rebuilds the connector with the new language. Text from T3
(prompts, plans, approval option labels) is shown as sent; the deck action
labels (Implement plan, Mark seen, ...), the tile state labels (PLAN READY,
STALE, ...) and the result messages of `send()` are still English only.

## Project favicons

With `[view].tile_icon = "project"` or `"both"`, a T3 tile shows the favicon T3
shows for its project: the project's `faviconPath` (the icon picked in T3)
when set, else what T3 discovers in `workspaceRoot`. After each refresh, outside
the delivery lock, the connector asks for every project it has not fetched in
30 minutes (10 minutes after a miss or failure) and hands the bytes to the
runtime's icon store; the next refresh puts the hash on the tile. Only PNG,
ICO and SVG up to 256 KiB are used (the bridge's limits); anything else (a
`.icns`, a larger file, a lucide `projectIcon`) keeps the monogram. A failed
re-fetch keeps the icon already shown.

## Server label

The descriptor also carries the server's display name (`"label":
"Jupiter-M4mini"`, next to `environmentId` and `serverVersion`). The connector
keeps it after each successful negotiation and reports it to the runtime once
per change (`on_server_label`). It is untrusted network text: only a string is
used, control and format characters are dropped, whitespace collapses, and it
is cut to 32 characters (the tile ellipsizes it further to fit).

The deck uses it as the tile's server tag (`"server"` in `[view].tile_fields`)
only when more than one T3 server is configured and that server has no
configured `[[servers]].label`; a single T3 server keeps the `T3` tag. The
deck's offline note and launcher line name a server by its configured label or
its id, never by the descriptor label.

## Known limits

- A never-opened completed thread is not Done (same as T3).
- Approval answers are offered only for live requests, and questions only when
  there is one single-choice question. Everything else says "Answer in T3".
- After an uncertain delivery the tile stays read-only until the thread
  changes; the deck offers no buttons meanwhile, and only a client holding the
  older revision (for example herdeck-ctl) can resend the same command.
- The desktop agent card shows a T3 rejected, uncertain or skipped result as
  a failure with the connector's message. The deck tiles and drill do not show
  the message yet.
- No raw terminal keys, streaming or desktop thread navigation.
- Subagent lists are one level deep: a subagent's own subagents live in the
  hidden child's detail, which Herdeck does not read. A parent that is not
  active (settled, snoozed, archived) is not re-read, so its tile keeps the
  last list it had.

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

`herdeck-doctor` identifies environments by the descriptor's `environmentId`
(fetched without a credential, 2 s timeout), not by URL, so a tunnel URL and
the server's own origin count as the same server. It warns when the T3 server
on this machine (from `~/.t3/userdata/server-runtime.json` `origin`, or the
older `host` + `port`) answers but no configured server reaches its
environment, and when two configured servers reach the same environment
(every thread would get two tiles). It does not probe other hosts.

See [agent-setup.md](agent-setup.md#dedicated-t3-transport-and-renewal-macos).
