# T3 compatibility implementation plan

Baseline: audit 2026-09-06, T3 0.0.38, Herdeck 430e11e.

1. Lifecycle and presentation: separate active/settled/snoozed/archived/deleted
   from execution and attention; retain inactive pinned tiles with truthful labels;
   queued start, plan ready, errors, snooze wake rules, local persisted Done acknowledgment.
2. Semantic commands: descriptor capability negotiation; lifecycle-aware revision;
   explicit lifecycle actions; plan implementation reference; explicit persistent
   approval confirmation; unsupported input guidance; distinct session stop.
3. Transport and operations: shell cache + selected detail refresh; per-thread fault
   isolation; read-only stale data; no write retries; same-ID credential renewal;
   dedicated launchd SSH forward and scheduled credential renewal on macBench.
4. Acceptance: fixtures from versioned upstream contracts; tests of the converged
   physical runtime; exact-source/package deploy; healthy Herdr + T3 connections;
   pin round trip and rendering from the actual runtime.

Network scope: reuse existing SSH access and Tailscale membership. No DNS,
Tailscale Serve/Funnel, public proxy or Cloudflare configuration changes.

Cross-device read markers are not exposed by T3's HTTP API. Herdeck acknowledgment
is deliberately local and is persisted per server/thread/completion identity.

## Implemented contract

- Inactive threads remain in source snapshots for local pins, but do not count
  as active work or occupy unpinned overview tiles. T3 pins never change deck pins.
- Acknowledgments identify the exact completion timestamp and survive restart in
  a per-server file under the config directory. New completions become Done again.
- The descriptor negotiates 0.0.31+ core commands and 0.0.38+ extended commands
  within the 0.0 release series. Unknown major/minor/prerelease versions are read-only.
  Settle/snooze additionally require their advertised capability flags.
- Implement plan uses the selected proposal reference and default interaction mode.
  Session stop is separate from turn interrupt. Persistent grants and implementation
  require two presses; Always grants additionally obey the existing safety switch.
- Every action re-reads its selected thread and checks the complete decision revision.
  Unchanged overview detail is cached for at most 15 seconds; shell fields and snooze
  time are evaluated on every poll. A failing detail is read-only; 404 removes that
  thread, while authentication failures take the connection offline.
- Accepted and uncertain writes are never automatically replayed. Unknown outcomes
  stay locked until the exact command effect is observed.

Credential renewal and live acceptance are tracked in the operational runbook.
