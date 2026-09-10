# T3 completion read-state investigation — 2026-09-07

> Historical snapshot from the 2026-09-07 investigation, preserved during
> repository cleanup on 2026-09-10. Statements below about upstream, live state,
> and the absence of an implementation describe that investigation, not the
> current integration. Herdeck subsequently added an opt-in local desktop
> read-state bridge; see the [current setup guide](../agent-setup.md#temporary-local-t3-desktop-read-state-sync-macbench).
> The original findings were not revalidated during archival.

Requirement: opening a completed thread in T3 must clear Herdeck Done without
requiring a second acknowledgement on the deck. No custom T3 fork.

## Verified evidence

- T3 v0.0.38 `apps/web/src/components/ChatView.tsx` calls `markThreadVisited`
  with the exact latest turn completion timestamp on opening/rendering a thread.
- `apps/web/src/uiStateStore.ts` stores `threadLastVisitedAtById` in browser
  localStorage under `t3code:ui-state:v1`. The Zustand subscription persists it
  locally (500 ms debounce). This action dispatches no server command.
- Current upstream main retains the local UI read-marker store. Desktop completion
  detection compares completedAt with lastVisitedAt; never-visited history is not
  automatically treated as unread. Herdeck currently has different semantics here.
- MacBench has the corresponding Electron Local Storage/leveldb directory in
  its t3code app support folder. A later targeted read-only check of the write-ahead log found the tested
  Ponk thread visit timestamp; no storage was modified.
- Read-only schema inspection of the live T3 server database found no thread visit
  timestamp. `provider_session_runtime.last_seen_at` is provider bookkeeping,
  not a user reading a conversation.
- Upstream issue #4952 remains open: mobile omits the desktop unread-completion
  presentation and equivalent visit tracking. Opening mobile threads must not be
  described as publishing a shared read receipt.

## Conclusion and boundaries

No supported shared read receipt was found. A thread-detail HTTP read is not proof
of a user visit: background fetches and Herdeck itself also fetch thread details.
Do not infer seen from those requests, use a timer, or claim local acknowledgments
are synchronized T3 state.

Reading the desktop's private store could only provide a device-local workaround;
it cannot observe mobile or another computer and needs careful live database
handling. It was not implemented or tested as a supported integration.
Cross-device equivalence requires an upstream shared read-marker mechanism (or
cooperation from each client). No runtime state, acknowledgement, or credential
was changed during this investigation.

## Sources

- https://github.com/pingdotgg/t3code/blob/v0.0.38/apps/web/src/uiStateStore.ts
- https://github.com/pingdotgg/t3code/blob/v0.0.38/apps/web/src/components/ChatView.tsx
- https://github.com/pingdotgg/t3code/blob/main/apps/web/src/components/Sidebar.logic.ts
- https://github.com/pingdotgg/t3code/issues/4952

## Live MacBench reproduction

The user opened Ponk.app / Stav repa a dalsi kroky on MacBench. Server status
fields were unchanged and Herdeck stayed Done. A targeted read-only scan of
the Electron LevelDB log found this thread in threadLastVisitedAtById with
2026-09-07T01:20:20.903Z, exactly matching its last completion. Thus the desktop
recorded the visit locally while Herdeck did not consume it. A local integration
is technically possible without a T3 fork. A production reader must resolve
current LevelDB state (sequence numbers, deletions, compaction and origin), not
select arbitrary historical matches from a log. The scan is diagnostic evidence,
not an implemented synchronization mechanism.
