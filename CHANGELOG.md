# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- `[usage].source = "auto"` no longer leaves the deck without limits when a
  bridge offers usage but sends no numbers (e.g. a LaunchDaemon bridge whose
  `codex`/`codexbar` cannot reach the login keychain). After 180 s without
  numbers the runtime falls back to its own poller and switches back once the
  bridge sends numbers; `/health` lists such bridges under
  `usage.bridges_empty`.

## [0.12.0] - 2026-09-24

### Changed
- Redesigned status panel (the D200's wide window, the two Elgato keys, the
  panel in the app and the web simulator). Every state now shares one layout:
  a state chip and a short fact on top, the main content in the middle, and
  what a press does plus page dots at the bottom. The calm overview shows
  count cards (or the usage gauges), **Needs you** fills the panel amber with
  the waiting agent's name, **Offline** and **Config error** fill it red, and
  the agent detail shows its status chip, who it is and the prompt. The "▲"
  markers and the `· 1/2` text suffix are gone. The panel is drawn at 2× and
  downsampled for smooth edges.

## [0.11.0] - 2026-09-24

### Added
- OpenCode subagent tracking: a shipped plugin (`herdeck-subagents.js`) reports
  OpenCode child sessions (subagents, found by their `parentID`) to
  `herdeck-subagent-hook --provider opencode`, which feeds the same per-pane
  spool, `⑂N` badge and agent card list as Claude Code and Codex.
  `herdeck-service hooks install|uninstall|status --agents opencode`, the
  bridge `hooks` message and the Maintenance switch install it into
  `~/.config/opencode/plugins/` (`$OPENCODE_CONFIG_DIR` / `$XDG_CONFIG_HOME`
  honoured). The installer only ever writes or removes that one file, only
  when it carries herdeck's marker, and backs it up first. A default `install`
  adds it only where OpenCode is set up. README "Subagent tracking".
- Subagent transcript fallback on the bridge: every 60 s, for panes whose
  spool still has running or stale subagents, the bridge reads the tail of
  the provider's transcripts and marks a subagent done or failed when its
  stop hook never arrived. For Claude Code that is a `task-notification` or
  `Agent` tool result in the parent transcript; for Codex, `task_complete` or
  `turn_aborted` in the child rollout. It then updates the `subagents` token
  through herdr (`pane.report_metadata`). Unknown formats leave entries as
  they are, and file reads are bounded and run off the event loop.
- `[notifications].subagents_done = false` (opt-in, editor field with en+cs
  help): one alert (`claude · subagents done (3)`, en+cs) when an agent's last
  running subagent finished and the agent is idle, blocked or done. It fires
  once per burst through the usual backends, `skip_focused` and a per-agent
  cooldown.
- Bridge lifecycle events (capability `events`): the bridge gives every
  blocked and done episode a stable `episode_id` (it survives a bridge
  restart), streams `event` frames (`blocked` with the pre-read, sanitized
  prompt and its revision, `done`, `unblocked`, `cleared`, `answered` with
  who answered) to clients that subscribe, and replays the last 30 minutes
  after a client's `seq`. An answer from any client marks the episode
  answered for all of them; a later answer that names the same episode is
  refused as `stale` unless the prompt changed. README "Lifecycle events".
- Runtimes (deck, web cockpit, Telegram) take their blocked/done alerts,
  reminders and banner withdrawals from a bridge that offers `events`, and
  stop detecting transitions locally for it, so no alert arrives twice. The
  blocked prompt comes with the event (no extra read for drill, card or banner
  excerpt). An answer on any client withdraws the banners elsewhere, closes a
  drill left open on that prompt, and makes banner and card answers for it
  return `stale`. Outgoing answers carry the bridge `episode_id`. Reminders
  count from bridge time. The last seen event per server is kept per runtime
  in `~/.cache/herdeck/bridge-events-<runtime>-<profile>.json`, so a
  restarted or woken runtime only alerts what it missed. A subscription the
  bridge does not confirm within 10 s falls back to local detection and is
  retried. Older bridges keep local detection.
- `herdeck-service hooks install|uninstall|status [--agents claude,codex]
  [--hook-path PATH] [--json]` installs the subagent hooks into
  `~/.claude/settings.json` and `~/.codex/hooks.json`. It adds only its own
  entries and leaves herdr, herdwatch, moshi and every other hook or setting
  alone. It backs up each file before a change, writes atomically, changes
  nothing on a second run and refuses a file that is not valid JSON. It
  reports Codex's `[features] hooks` and the `/hooks` trust step but never
  enables the feature. The bridge exposes it as the `hooks` message
  (capability `hooks`, full token only), the runtime as `GET`/`POST
  /maintenance/servers/<id>/hooks` plus a `hooks` summary per server in
  `GET /maintenance`, and the desktop Maintenance section as an on/off switch
  per agent under each bridge, with a confirmation first (en + cs).
  `herdeck-doctor` reports the hook state per agent.
- The bridge can poll provider usage itself: `herdeck-service install bridge
  --usage` (or `HERDECK_BRIDGE_USAGE=1`, settings from the `[usage]` table of
  `HERDECK_USAGE_CONFIG`) runs the usage poller on the agents' Mac and pushes
  `usage` frames (capability `usage`) to every runtime on connect and on
  change. Thin-client decks no longer need SSH wrapper scripts for
  `codex_path` / `codexbar_path`.
- `[usage].source` (`auto` default, `local`, `bridge`; editor: Usage →
  `source`): `auto` prefers a connected bridge that offers usage and falls
  back to the local poller (also for older bridges). `providers` / `paid_only`
  are applied by the runtime to bridge data too, alerts and the pace hint work
  from either input, and several bridges merge per provider in config order.
  `/health` reports the active input under `usage`.
- Subagent badge: an agent tile shows `⑂N` in its bottom band while the pane's
  agent has N subagents running, on the D200, the desktop deck window, the web
  simulator and the Elgato plugin. The new `herdeck-subagent-hook` command,
  run as a Claude Code (`SubagentStart`/`SubagentStop`, `PostToolUse` on
  `Agent`, `PreToolUse` heartbeat) or Codex (`SubagentStart`/`SubagentStop`)
  hook, keeps a small per-pane spool in `~/.cache/herdeck/subagents/` and
  reports the Herdr metadata token `subagents=<running>/<total>`. The runtime
  reads that token into the agent state. Add the hooks with
  `herdeck-service hooks install`, from Maintenance, or by hand (README
  "Subagent tracking").
- Status history and statistics. The bridge records every status stretch of
  every agent pane in a local SQLite store
  (`~/.local/state/herdeck/history.sqlite`, 0600, 30 days, at most 200,000
  rows), including whether a blocked stretch ended after an answer sent
  through herdeck. A new `stats` message (capability `history`, read-only
  token allowed) returns working/blocked/idle/waiting time, blocked count,
  answers, median and p90 time to answer, done count, per group (agent, repo,
  agent type) and per day for today, 7 or 30 days. The runtime serves it as
  `GET /stats?range=&group=`, summing all connected bridges, and the desktop
  app has a new Statistics view (Settings → Control) with a summary row, a
  per-day chart and a per-group table, in English and Czech.
- Subagent list on the desktop agent card. The bridge reads each pane's
  subagent spool (cached by file mtime, capped at 20, corrupt or oversized
  files ignored, the hook's stale/drop rules applied at read time) and adds a
  `subagents` list to the wire pane (capability `subagents`). The agent card
  lists them newest first with type, description, status (running, done,
  failed, no signal) in colour, a live running time or the total time, and
  nested subagents indented (en + cs). The section is hidden when there are
  none. The cockpit's `GET /api/v1/agents` records gain the same `subagents`
  list.

## [0.10.2] - 2026-09-24

### Changed
- Desktop health notices redesigned. The app window shows one compact row per
  problem (config does not load, bridge disconnected or token rejected, version
  mismatch, D200 disconnected or held by another runtime, unsupported bridge
  protocol) with a severity bar and icon (error red, warning amber, info blue),
  one human sentence in the UI language (en/cs), one fix button where there is
  one (Update bridge, Restart deck, Restart runtime, Fix config…), a Details
  link to Settings → Maintenance and a × that hides the notice until that
  problem changes (a new outage, other versions). A new app release is an info
  row with Install and restart. Raw backend errors moved to the tooltip. The
  Maintenance entry in the settings navigation shows a problem-count badge.
- The deck window shows no notice rows any more, only a small status dot in the
  corner (coloured by the worst problem, tooltip with the top one); clicking it
  opens Maintenance in the app window. Updates are installed from the app
  window.
- Action results (bridge update, restart deck/runtime, update checks, install
  errors) are toasts in the corner: successes leave after 5 s, errors stay
  until closed, and a bridge update shows its progress as a stepper
  (download → install → verify → restart).

### Fixed
- A T3 server that has never answered (configured but not running) no longer
  shows up as "disconnected": the T3 connector now reports `ever_connected`
  (and `since`, `attempt`, `last_error`) on `/health` and `/maintenance` like
  the herdr connector, and Maintenance lists such servers greyed out as
  "not in use".

## [0.10.1] - 2026-09-24

### Added
- `[[servers]]` entries accept `token_file = "<path>"` (`~` expanded, content
  stripped) next to or instead of `token_env`. Tokens resolve from the
  `token_env` environment variable, then the token file, then the keychain
  entry named by `token_env`. The file must be a regular, owner-only (`0600`)
  file; anything else is refused with a config error. Works for herdr and T3
  servers and is editable in the Connections editor. `herdeck-doctor` reports
  which source resolved each server's token (never the value).

### Fixed
- A config file that exists but cannot be loaded (for example a runtime service
  without the shell env its `token_env` needs, or a missing keychain entry) no
  longer starts the demo fleet silently. The runtime shows an explicit error
  state: empty tiles and a `CONFIG ERROR` panel (en/cs) on the D200 and in the
  window, `config_error` on `/health` and `/maintenance`, `source:
  "config_error"` in `runtime.json`, and an ERROR log line. HealthNotice and
  the Maintenance section show the message. The runtime watches the config and
  its token files and re-checks every 10 s, so a fix recovers the deck without
  a restart. The demo still starts with `HERDECK_MOCK`, the demo choice in
  onboarding, or no config at all.
- A running remote deck whose config breaks on reload now shows that error
  instead of switching to the demo fleet.
- The deck's "time in this status" timers no longer reset to 0 whenever the
  runtime restarts (deploy, config reload, app update, source swap). The
  bridge now stamps each pane with `status_since_ms` (unix ms when it entered
  its current status, keyed on pane id + terminal id, advertised as the
  `status_since` capability) and persists the table to
  `$XDG_STATE_HOME/herdeck/bridge-status-since.json` (0600; the embedded local
  bridge uses `local-bridge-status-since.json`), so a bridge restart keeps
  the clocks of panes whose terminal and status are unchanged. The runtime
  prefers that timestamp for tile times and blocked ordering, falls back to
  its first-seen time for older bridges, and ignores a bridge clock more than
  5 s in the future. A source swap also keeps the local fallback times.

## [0.10.0] - 2026-09-24

### Added
- `GET /maintenance` now reports per server `self_update` (the bridge
  advertises the capability) and `managed` (`true`/`false` from one health
  probe per connection; `null` while unknown, on timeout or for older bridges).
- Bridge self-update. A bridge running from a managed venv (a `managed.json`
  marker at `Path(sys.prefix)`, written by `herdeck-service install bridge
  --managed`) accepts a new full-token-only `update {req, version}` message:
  it downloads the release wheel, checks it against the release `SHA256SUMS`
  (a missing wheel is an error), installs it with pip or uv, imports
  `herdeck.bridge` and checks the installed version in a fresh interpreter,
  answers and exits for its service to restart it. It refuses a downgrade
  unless `allow_downgrade` is set, and never installs a version older than
  0.10.0 (the first with self-update). Progress streams as
  `progress` frames; any failure keeps the old version running and reports
  the installer's output tail. The health probe reports `managed`, and the
  bridge advertises the `self_update` capability.
- The runtime's `POST /maintenance/servers/{id}/update` (and `GET` of the same
  path to long-poll a running update) asks a bridge to update itself to the
  runtime's version and answers `updated`, `pending`, `current`, `newer`,
  `not_managed`, `readonly`, `failed`, `downgrade`, `busy`, `unsupported` or
  `disconnected`. A bridge already at or above the runtime's version is never
  sent an update.
- Tag releases publish the Python sdist and wheel plus a `SHA256SUMS` file.
- Runtime maintenance API for the upcoming desktop Maintenance section:
  token-authenticated `GET /maintenance` (versions, runtime service, log paths,
  D200 state incl. USB presence and last-seen hub port, per-bridge health),
  `POST /maintenance/deck/restart` (close + reopen the D200 and repaint a full
  frame, never releasing `d200.lock`; reports `locked_by <pid>` when another
  runtime owns it) and `POST /maintenance/deck/power-cycle` (`uhubctl -l <hub>
  -p <port> -a cycle -d 2`, reporting `needs_admin` with the exact command when
  it needs root).
- The D200's USB hub location is remembered in
  `$HERDECK_RUNTIME_DIR/d200-usb.json`; `[hardware].uhubctl`, `usb_hub` and
  `usb_port` configure the power-cycle.
- Config keys for two env-only switches (the env vars remain a fallback):
  `[hardware].d200_standard_writer` and `desktop_read_state` on a T3
  `[[servers]]` entry.
- `herdeck-service`: `--env KEY=VALUE` (non-secret launch environment),
  `install bridge --managed [--version X]` (release install into its own venv
  with a `managed.json` marker), `restart <kind>` and `status <kind> --json`.
- The desktop app's bundled runtime binary runs the service CLI as
  `herdeck-deckapp service ...`, so the runtime service can be installed from
  the app without Python.
- Desktop **Maintenance** section (Settings → System → Maintenance): app,
  runtime and bridge versions with mismatches flagged; where the runtime comes
  from (service from this app, service from a checkout, inside the app, or
  started elsewhere) with **Run as a service from this app**, **Restart
  runtime**, **Remove service** and **Open runtime/app log**; the D200 state in
  words with **Restart deck** and **Power-cycle USB port** (disabled with the
  reason when uhubctl or the hub port is missing; `needs_admin` shows the exact
  command with a copy button); and per bridge an **Update bridge** button (only
  for a managed, self-updating bridge older than the runtime) with live
  progress and an explanation for every outcome. A bridge that is not a managed
  install shows the one-time `herdeck-service install bridge --managed --version
  <runtime version>` command; an unknown install type (older bridge, T3) gets a
  neutral note.
- HealthNotice offers the fix inline: **Update bridge** on a version mismatch
  of such a bridge, **Restart deck** on a D200 problem, **Open Maintenance**
  otherwise.
- Tray item **Restart deck** and an optional `[hotkeys].restart_deck` global
  shortcut (default off).
- Editor fields for `[hardware].d200_standard_writer`, `uhubctl`, `usb_hub`,
  `usb_port` (Deck → Advanced) and a T3 server's `desktop_read_state`
  (Connections).
- Desktop shell commands `maintenance_call` (token-injecting proxy for an
  allow-list of exact `/maintenance*` routes; the bridge-update read timeout
  outlasts its wait), `open_log` (only the runtime log `/maintenance` reports or
  the app's own log, as an existing `*.log` under the log directories) and
  `runtime_service` (the bundled runtime's `service install|restart|uninstall|
  status runtime`, with a timeout).

## [0.9.0] - 2026-09-24

### Added
- Actionable macOS banners. Clicking an agent banner brings the deck forward
  and opens that agent's drill (new token-authenticated `POST /agents/drill`).
  Opt-in `[notifications].banner_actions` adds **Approve**/**Deny** to a blocked
  banner whose prompt is a plain permission question, or an inline **Reply**
  field otherwise (`POST /agents/answer`); an answer is applied only while the
  agent is still blocked in the same episode on the same options, so a stale
  banner opens the drill instead. Opt-in `[notifications].banner_prompt`
  appends a short, sanitized prompt excerpt to blocked alerts. Both are
  editable under Notifications.
- Stale banners are withdrawn: when an alerted agent is answered anywhere,
  starts working again or disappears, the desktop app removes its delivered
  banners from Notification Center (a new `withdraw` feed item, only sent to a
  shell that announces support).
- No local banner for the pane herdr reports as focused while you are using
  the deck host (input within 2 minutes; an unknown idle time counts as away);
  Telegram alerts are never skipped for it. `[notifications].skip_focused =
  false` turns this off. The bridge now passes herdr's `focused` flag on the
  wire.
- Opt-in `[notifications].remind_after` (minutes): an agent that stays blocked
  alerts again once per interval, at most three times per episode.
- Opt-in `[notifications.telegram].only_when_away` (minutes): Telegram alerts
  only after that long without input on the deck host (macOS) and without a
  deck press.
- Desktop agent card: Option-click, long-press or ⌥digit a tile in the deck
  window to see that agent in full (header, status and since, the whole blocked
  prompt, the drill's parsed options, a free-text reply, Focus and Stop). New
  token-authenticated runtime routes `GET /agent/detail` and
  `POST /agent/{answer,text,stop,focus}`; an answer carries the prompt revision
  the user saw and is refused as stale when the prompt changed. Card actions
  wait for the bridge's reply, so a read-only bridge token is reported instead
  of being dropped silently (bridge `error` frames now keep their `req`).
- Live terminal in the desktop agent card: a read-only xterm.js view of the
  agent's pane, relayed from the bridge's `observe` through new runtime routes
  `POST /agent/term/open`, `GET /agent/term/poll` (bounded long-poll) and
  `POST /agent/term/close`. Observation stops when the card closes, the
  terminal is toggled off or the window hides; the runtime also stops previews
  nobody polled for 15 s and runs at most two at once.
- `herdeck-bridge --rotate-token [--token-file PATH] [--show]` writes a fresh
  random `0600` token atomically and prints the next steps (restart the
  bridge, update the runtime-side token); the token is printed only with
  `--show`.
- Optional read-only bridge token (`HERDECK_READONLY_TOKEN_FILE`): clients using
  it get snapshots, icons, pane reads, live previews and health, and every
  other message (`act`, `focus`, `refresh_title`, `send_text`,
  `choose_if_blocked`, `start`, unknown types) is rejected with an error.
- Version handshake: bridge snapshots carry `herdeck_version`, and the runtime
  `/health` reports its own `version` and `protocol` plus each server's
  `bridge_version`. The desktop window shows a small warning when the runtime
  differs from the app or a bridge from the runtime
  (`runtime 0.8.0 ≠ app 0.8.1 — restart the runtime`), and `herdeck-doctor`
  reports versions and mismatches. A bridge speaking a newer wire protocol is
  now logged as a WARNING and flagged `protocol_supported: false` instead of
  rendering blank in silence.
- `/health` explains a dark deck: per-server `connected`, `last_error`, `since`,
  `attempt`; the D200 sink's `connected`, `last_frame_at`, `last_error` and
  `lock_owner` (another runtime holding `d200.lock`); `pid`, `uptime_s`; and
  notification counters (`queued`, `acked`, `fallback`, `dropped`, `pending`).
  The window shows a concise line when something is wrong
  (`bridge local: token rejected 3 min · D200: disconnected 2 min`).
- Bridge health: an authenticated `{"type": "health"}` WebSocket message
  returns the bridge's version, wire protocol, whether herdr answers and the
  number of attached clients; `herdeck-doctor` uses it.
- Opt-in usage-limit notifications: `[usage].alert_at` (used-% levels, e.g.
  `[80, 95]`) notifies once per limit window when a provider window crosses a
  level, and `[usage].alert_reset` announces when a window that reached 100 %
  (or the highest level) resets. They go through the `[notifications]`
  backends (macOS banner via the desktop shell, Telegram) with the `done`
  sound; the first poll after startup is a silent baseline. Editable under
  Usage.
- The usage detail on the status panel shows a pace hint (`full ~40m early`)
  when the recent burn rate would fill a window before it resets.
- Triage loop: pressing the `▲ needs you` status panel opens the drill of the
  agent blocked longest; answering (or stopping) it moves straight on to the
  next-longest blocked agent, and back to the overview when none is left.
  Back still returns to the overview. The desktop app gets an opt-in global
  shortcut, `[hotkeys].next_blocked` (no default), that shows the deck and
  does the same through the runtime's new `POST /triage` route; pressing it
  again skips to the next blocked agent.
- Opt-in `[local].terminal_app` (for example `"Ghostty"`): after a tile press
  focuses an agent's pane, the deck machine brings that app forward with
  `open -a` (macOS only, off the event loop, failures only logged). Only useful
  when the herdr client runs on the deck machine.
- Opt-in `[view].collapse_idle`: idle agents fold into one `+N` tile at the
  end of the overview, so 15-20 agents fit without paging; pressing it
  unfolds them and a `hide` tile folds them back (also after a minute idle).
  Pinned agents keep their tile. D200, desktop deck window and web simulator;
  the Elgato plugin ignores it. Editable under View.
- The desktop app keeps a log when launched outside a terminal: its own and
  the sidecar runtime's stderr, timestamped, in
  `~/Library/Logs/herdeck/herdeck.log` (Linux: `~/.local/state/herdeck/`),
  rotated at 5 MB. The runtime now also logs each notification's route
  (queued for the banner, or the `osascript` fallback) at INFO.
- `herdeck-service install runtime` runs the deck runtime as a service:
  a login-session LaunchAgent (`dev.herdeck.runtime`, KeepAlive, log in
  `~/Library/Logs/herdeck-runtime.log`) or a systemd `--user` unit on Linux,
  where the `bridge` and `web` kinds now install too. `--from-app
  [/Applications/herdeck.app]` runs the frozen runtime bundled in the desktop
  app; the app's updater then restarts that unit when it installs an update,
  so runtime and app stay on one version (units running from a source
  checkout are left alone).
- `scripts/deploy-host.sh`: one command to deploy a committed ref to a
  runtime or bridge host that runs from source (ssh or local) — immutable
  snapshot, `pip install -e`, service restart, `/health` or TCP check, and a
  printed `--rollback` command on failure. It replaces the manual recipe in
  `docs/updating-a-deployment.md`, which now also covers moving a `nohup`
  bridge under `herdeck-service`.
- Every `osascript` notification fallback is logged at WARNING with its
  reason: `reason=no_shell_claim last_claim_age=…s` (or `never`) when no app
  claimed banner duty in the last 60 s, `reason=shell_native_failed error=…`
  when the app's native banner failed. The runtime also logs when the app's
  banner claim is acquired, moves to a relaunched app, or lapses.
- The desktop app logs which runtime it uses and why:
  `herdeck: runtime plan=attach|spawn reason=…`, at launch and on every switch.

### Changed
- `herdeck`, `herdeck-web` (and `python -m herdeck.app`) now run the same
  runtime as the desktop app and `herdeck.runtime` (DeckApp + LiveSource),
  with the web cockpit, D200, Elgato USB or headless deck attached as a
  front. Routes, cookies, CSP/frame headers, the `/api/v1` cockpit API,
  `/term` streams and Telegram alerts are unchanged (pinned by
  `tests/test_contract_*.py`). What differs:
  - A restart no longer re-alerts every agent that is already blocked; only
    transitions after the first snapshot alert (Telegram included).
  - Alerts follow the runtime's notification rules everywhere: macOS
    banners carry the project icon, `[notifications].skip_focused`,
    `remind_after` and `[notifications.telegram].only_when_away` apply to
    these hosts too.
  - `HERDECK_DECK=d200` waits for the device (and for `d200.lock` held by
    another runtime) instead of exiting, and reopens it after sleep/USB loss.
    Auto-detect skips a D200 another runtime owns.
  - A config edit or deck profile switch reconnects to the bridge; Telegram
    buttons sent before it keep working. A failed reload, a locked profile
    or a failed pin save shows a short status panel (now also in Czech, and
    also in the desktop runtime).
  - `HERDECK_MOCK=1` shows the desktop app's demo fleet (static; a press
    cycles a tile) instead of the old cycling five-agent demo.
- `herdeck-bridge` now refuses to start unless `HERDECK_BIND` is loopback or a
  Tailscale address, like `herdeck-web` and `herdeck-service` already did;
  `HERDECK_ALLOW_UNSAFE_BIND=1` overrides it.
- Blocked agents are ordered by how long they have been waiting, longest
  first, instead of by pane id: on the overview (D200, desktop window, web
  simulator) and in the Elgato pager. With `agent_order = "status"` the wait
  also beats server order; with `"herdr"` the Herdr position still comes
  first.
- Tiles and panels are drawn with a bundled font (Inter 4.1, SIL OFL 1.1, in
  `src/herdeck/assets/fonts`) instead of whatever the OS has (Arial/Helvetica on
  macOS, DejaVu/Liberation on Linux), so a tile wraps, shrinks and truncates the
  same on every machine and in the packaged apps. Text looks slightly different
  from before; cached tile images are re-rendered once.
- The Elgato plugin's agent tiles now honour `[view].working_animation` in
  their tile data (they are built by the same code as the deck's).

### Fixed
- `herdeck.runtime` installs its SIGTERM/SIGINT handlers before it publishes
  `runtime.json`, so a stop sent right after start-up no longer leaves a stale
  discovery file behind.
- A profile switch, config reload or demo-to-live connect no longer drops macOS
  alerts to the plain `osascript` fallback: the swapped-in source keeps posting
  through the desktop app.
- A sidecar spawned by the desktop app no longer outlives it. It used to keep
  running (and holding the D200) after a crash, SIGKILL or Force Quit. It now
  exits cleanly when the app's stdin pipe closes, with a parent-pid check as a
  fallback. A normal quit closes the pipe and kills the sidecar only after 5 s.
- Two runtimes no longer fight over one D200. The owner holds
  `~/.cache/herdeck/d200.lock` (`$HERDECK_RUNTIME_DIR` respected). Another
  runtime leaves the device alone, keeps serving its window, and takes over
  when the owner exits.
- A desktop app that spawned its own sidecar because the launchd runtime
  was not answering at launch (e.g. right after an auto-update relaunch) now
  switches to that runtime once it is healthy and stops its sidecar. It
  checks every 12 s and after any failed poll. Before, the two runtimes stayed
  until the app restarted, and banners fell back to `osascript`. If the
  runtime it switched to then stays unreachable (3 failed re-discoveries over
  at least 30 s), the app starts its own sidecar again.
- SVG project favicons now render in the packaged desktop app and the Elgato
  plugin (they showed the monogram): SVG goes through resvg (`resvg-py`, a
  self-contained wheel bundled into both). An SVG favicon referencing external files or
  URLs is refused and shows the monogram.

### Removed
- The legacy `herdeck.app.App` runtime and its copies of the notification,
  Telegram, cockpit-API and terminal-preview logic. `herdeck.app` remains only
  as an alias entry point (`python -m herdeck.app`, `from herdeck.app import
  main`); `scripts/e2e_verify.py` runs the one runtime.
- The cairosvg dependency: resvg is now the only SVG rasterizer, at runtime
  and when baking the bundled glyph PNGs. Source installs and builds no longer
  need the native cairo library (`brew install cairo`).
- `deploy/com.herdeck.app.plist`: it launched the legacy `herdeck.app` with an
  inline token and no log path. Use `herdeck-service install runtime`.

## [0.8.0] - 2026-09-23

### Added
- Project favicons on agent tiles: `[view].tile_icon = "project"` shows the
  repository's favicon instead of the agent mark (`"both"` adds it as a small
  badge; default `"agent"` is unchanged). In `"project"` mode the `spin`
  working animation renders as a `comet` ring around the favicon. The bridge discovers the icon in the
  pane's repo and sends it once per content hash to deck runtimes that opt in
  (new `project_icon` capability); `[view.project_icons]` overrides it per repo
  with a file on the deck machine. PNG and ICO work everywhere; SVG only where
  cairosvg is available (not the packaged app or Elgato plugin). Projects
  without a usable icon get a coloured letter badge. Editable in the desktop
  settings (View).
- Project favicon discovery for panes whose cwd is not inside a Git repo: the
  bridge checks that folder's own favicon candidates, then those of its direct
  child repos in name order (capped at 32 child directories; never for `$HOME`,
  its ancestors or `/`), with the same 60 s restat.
- macOS notification banners carry the agent's project mark (favicon, a
  `[view.project_icons]` override, or the letter badge). The runtime writes it
  as a PNG under `~/.cache/herdeck/notification-icons/` (at most 64 files) and
  the desktop app attaches it as the banner's content image and, where macOS
  still honours it, its app icon. The `osascript` fallback stays text-only.

### Changed
- Usage panel: `paid_only = true` no longer skips the CodexBar fallback. Its
  `loginMethod` (e.g. "Claude Max 20x", "pro") now marks a result as paid;
  only recognised paid tiers are kept, unknown or free ones stay hidden.
- Notifications: `[notifications].on` now defaults to `["blocked", "done"]`
  (was `["blocked"]`), so a configured done sound actually plays. An explicit
  `on = ["blocked"]` still turns done alerts off; the settings editor now warns
  next to a sound whose event is not in `on` and offers to add it.

### Fixed
- Usage panel: the Codex app-server handshake gets its own 60 s start timeout
  (later requests keep 15 s), so a cold codex or an SSH `codex_path` wrapper no
  longer times out on every poll. Notifications and server-initiated requests
  interleaved with responses are skipped. README documents the thin-client
  setup (SSH wrappers for `codex_path` / `codexbar_path`).
- macOS notification sounds now ride on the banner itself instead of a
  separate `afplay`, so Focus / Do Not Disturb silences both; previously a
  muted banner still played its sound.
- Desktop banners are posted fire-and-forget: a click is caught by a single
  forwarding Notification Center delegate instead of one parked thread plus a
  0.5 s main-thread `deliveredNotifications` poll per banner, which leaked for
  every banner left in Notification Center, kept the main thread busy and,
  after 16 banners, stopped clicks from revealing the deck.
- Notifications: localized titles (`claude · needs input`, `claude · done`);
  per-agent cooldown so flapping agents stop spamming, and no **done** alert
  right after you answered that agent on the deck; the delivery feed holds 50
  items and never drops an undelivered alert silently.
- Desktop notification pump no longer spins the CPU, and backs off when the
  runtime has no notification feed (demo/mock).
- A headless runtime driving only the D200 no longer renders animation frames
  nobody reads; tile rasterization runs outside the state lock, so `/state`,
  tiles and presses are not stalled by a cold frame.
- `/state` supports long-polling (`?after=<version>&wait_ms=<ms>`) and the
  desktop keeps its HTTP connection alive instead of polling.
- A socket left behind by a crashed herdr is no longer reported as a running
  local session.
- An idle drill, launcher or profile menu returns to the overview after 60 s;
  while drilled, newly blocked agents are flagged on the panel; the launcher
  shows which server a new agent starts on when several are configured.
- Tile legibility and contrast improvements, accessible tile descriptions
  (`/state` `tile_labels`), clearer Czech status wording (blocked is now
  BLOKOVÁN), and translated drill fallback buttons.
- Settings UI fixes, including localized `/setup` errors via stable error
  codes and surfaced global-shortcut registration failures.

## [0.7.8] - 2026-09-22

### Fixed
- Native macOS notification delivery now stays active while Herdeck is in the
  background, preventing the AppleScript fallback and its generic icon after
  the desktop app has been idle.

## [0.7.7] - 2026-09-22

### Fixed
- Runtime notification acknowledgements now include queue-to-delivery latency,
  so delayed or duplicated desktop alerts can be diagnosed from the persistent
  MacBench log even when the desktop app is launched by LaunchServices.

## [0.7.6] - 2026-09-22

### Fixed
- Repeated bridge snapshots no longer leak the latest spinner phase and elapsed
  label into physical D200 frames. Volatile-only updates stay byte-identical and
  are suppressed by the driver, while real working/status/content transitions
  still repaint immediately.

## [0.7.5] - 2026-09-22

### Fixed
- Agent completion and blocked alerts now wake the desktop through an
  acknowledged long-poll feed instead of waiting up to ten seconds for the
  next state heartbeat. The desktop owns both banner and sound, correlates
  delivery latency in logs, and falls back to `osascript` without replaying or
  reordering queued alerts.
- A fresh runtime seeds its notification baseline from the first snapshot, so
  agents that were already done or blocked before restart do not alert again.
- Periodic spinner and elapsed-time ticks no longer upload full frames to the
  physical D200. Semantic changes, including an expired temporary panel, still
  repaint the device, avoiding the whole-deck flash seen while agents work.

## [0.7.1] - 2026-09-21

### Fixed
- The desktop shell now re-reads the runtime's `runtime.json` when its `/state`
  calls fail (health-checked, rate-limited): an externally managed runtime
  picks a fresh port on every restart, and a shell holding the stale port went
  silent — the runtime never saw the shell's banner claim, so event alerts
  fell back to plain `osascript` banners alongside the native ones
  (duplicated notifications). Native banners and the deck UI now survive
  runtime restarts without relaunching the app.

## [0.7.0] - 2026-09-21

### Added
- Event notifications now speak with Herdeck's own voice on macOS: the deck
  shell (Tauri) requests macOS notification permission at startup and posts
  the runtime's event alerts as native **Herdeck banners** (app name + icon),
  replacing the anonymous `osascript` banners whenever the shell is running.
- A notification heartbeat inside the shell keeps the claim (and the banner
  posting) alive even when both deck windows sit hidden in the tray.
- New `[notifications.sounds]` per-event system sounds keep playing from the
  runtime itself (audio is not app-attributed), and the per-event Telegram
  notifications keep working exactly as configured in
  `[notifications.backends]`.

### Changed
- The runtime records every event alert in a notification feed (`notify` in
  `/state`) instead of shelling out to `osascript` unconditionally; when no
  shell claims banner duty (older shell version, or the app is closed), the
  historical plain-AppleScript banner remains as a fallback.
- Handshake: shell polls carry `X-Herdeck-Shell` + per-process
  `X-Herdeck-Shell-Gen` headers; a new generation (app relaunch) resets the
  runtime's feed so already-delivered alerts are never replayed.

## [0.6.0] - 2026-09-21

### Added
- Notifications can now fire on the **done** state too (`on = ["blocked", "done"]`).
  Each event plays its own macOS system sound by default (Glass for blocked,
  Hero for done), overridable per event via a new `[notifications.sounds]` table
  with fields in the settings UI (including profile overrides). Done alerts
  stay one-way (no Telegram approve buttons).
- Agent tiles now show a non-empty Herdr pane title on their secondary line,
  falling back to the existing tab and branch context when no title is set.
  `title` is also available as an explicit tile-line token.
- Non-blocked Herdr agent details now offer a **Retitle** action when the
  `zhangzujian.auto-session-title` plugin advertises its pane refresh action.

## [0.5.0] - 2026-09-07

### Added
- T3 Code connections alongside Herdr, with project and thread titles,
  completion states, and capability-aware thread controls.
- CLI connection setup and credential renewal that preserve connection identity
  and verify adoption by the running runtime.
- Persistent deck pins, a choice of prominent project or thread titles, and
  a setting to show or hide source labels.
- Optional local T3 desktop read-state synchronization on the deck host. This
  temporary bridge requires LevelDB and does not synchronize other devices.

### Fixed
- T3 lifecycle handling for completed, settled, archived, and snoozed threads;
  uncertain actions are reconciled without blindly replaying writes.
- Long project names fit tiles better, with distinct title typography and
  smaller source labels.
- D200 status panels retain their native proportions.
- Missing pinned threads are distinguished from unavailable servers.

## [0.4.0] - 2026-09-04

### Added
- Agent tiles now ship polished offline marks for every interactive agent kind
  supported by Herdr 0.8.2: Amp, Antigravity, Claude, Cline, Codex, Copilot,
  Cursor, Devin, Droid, Gemini, Grok, Hermes, Kilo, Kimi, Kiro, Maki, Mastra
  Code, OMP, OpenCode, Pi, Qoder, and Qwen. Custom agent types retain the
  generated-letter fallback and local PNG overrides.

### Fixed
- Source installations without CairoSVG now load the committed pre-rendered
  agent marks instead of silently degrading bundled SVG brands to letters.

## [0.3.1] - 2026-08-31

### Fixed
- Codex usage polling now refreshes the cached ChatGPT access token before
  reading rate limits, preventing repeated `401 token_expired` failures after
  an otherwise valid Codex login ages.

## [0.3.0] - 2026-08-31

### Added
- Optional `[view].agent_order = "herdr"` mirrors Herdr workspace and tab
  positions within each status/server group while preserving attention priority.
- **Check for updates** in the tray. It opens the app window and answers either
  way — a newer release, "you are up to date", or the failure and its reason.
- **Qwen Code** joins the agent kinds herdeck can launch through herdr's
  managed start.
- Agent tiles now show herdr's own per-status label when one is set, so the
  deck and herdr's own sidebar always agree on the wording. herdeck's own
  explicit **waiting** label still takes precedence, and the generic status
  word remains the fallback when herdr has no label for that status.
- A one-time startup warning when the connected herdr predates 0.7.4, naming
  the consequence plainly: the **waiting** state will silently never appear.
- A warning is now logged whenever herdr reports that a pane read omitted
  older terminal rows, so a decision made on a clipped prompt is diagnosable
  instead of invisible.

### Changed
- The stated herdr requirement rises from 0.7.2 to 0.7.4: the metadata tokens
  herdeck's **waiting** state is derived from shipped in that release.

### Fixed
- The update check ran once per window mount and swallowed every failure
  silently, so a tray-resident app could sit on an old version indefinitely with
  no way to retry and no way to tell "up to date" from "the check broke". A
  check you asked for now always reports; automatic ones stay quiet, so an
  offline start still cannot disturb the deck.
- Answering a blocked agent from the deck — the drill's numbered-answer submit
  — stopped working against herdr 0.8.2, which now rejects a prompt answer for
  an agent already sitting at an approval or question dialog. herdeck now
  answers through herdr's pane-level primitives instead, so this works again.
  Because those type raw keystrokes with no paste framing, an answer that is
  empty, whitespace-only, or contains an embedded newline or control character
  is now refused outright rather than sent half-formed.
- A managed agent start no longer fails outright when the connected herdr does
  not recognize the agent kind; herdeck falls back to starting it by typing
  into the pane, as it always did before managed start.

## [0.2.0] - 2026-07-29

### Added
- The desktop app is built around two windows with fixed roles: an app window
  that hosts the control room, and a borderless deck window you pop out of it.
  Show the deck from the app window's toggle, the tray, or `CmdOrCtrl+Shift+D`;
  close it the same ways, with ⌘W, or from the deck's own right-click menu,
  which also carries the zoom steps and their shortcuts.
- `[desktop].deck_always_on_top` keeps the deck above other windows, applied
  live from the editor or the tray with no restart.
- Which windows were open, and where the deck sat, are remembered between
  launches in `~/.cache/herdeck/window-state.json`.
- The settings window, onboarding and floating deck are rebuilt around one token
  layer in which herdeck's own status palette is the semantic colour system, so
  the window and the physical Stream Deck agree by construction.
- Live terminal preview in the browser deck: long-press, right-click, or use
  `Shift+Enter` on an agent tile to watch that pane read-only, streamed from
  herdr (`terminal session observe`, herdr >= 0.7.3) through the bridge.
- Generic work item/run metadata in snapshots and deck/Telegram views.
- Production web and bridge service commands with private token-file support,
  health probes, reverse-proxy path prefixes, and explicit HTTPS embed policy.

### Changed
- Browser capability URLs now exchange into expiring HttpOnly sessions and
  redirect to clean URLs; browser writes require an exact origin match.
- The bridge now bootstraps fleet state from herdr's `session.snapshot` API and
  subscribes to tab/workspace/worktree events, so workspace, tab, and branch
  labels update instantly. **herdr >= 0.7.2 is now required** — check with
  `herdr status`, upgrade with `herdr update`.
- **`[desktop].window_mode` is replaced by `[desktop].deck_always_on_top`.**
  The old three-valued key decided four unrelated things at once and forced a
  process restart on any switch involving `normal`; nothing restarts now.
  Migration is automatic and happens once: `normal` opens the app window,
  `floating` and `always_on_top` open the deck, and `always_on_top` also turns
  the new flag on. The legacy key is read as a fallback and then ignored — it is
  deliberately left in your `config.toml` rather than rewritten.

### Fixed
- Herdr 0.7.5 protocol compatibility for submitted prompts and agent launches,
  while preserving protocol 16 and custom argv-based start profiles.
- The floating deck opened on the primary monitor rather than the one in use,
  and could be placed under the dock or the menu bar. It now lands in the work
  area of the screen the pointer is on, on Windows and Wayland too.
- A compact deck that could not reach the runtime showed a grid of blank keys
  and no reason why.
- The status panel spans two grid columns plus the gap between them, so its 2:1
  artwork set the deck's row height and hung a few pixels below the tiles.
- A press outline was never cleared, so the last-pressed key kept its ring while
  the agent under it changed; the deck card's shadow pooled in its rounded
  corners; and the navigation strip stretched on narrow screens.

## [0.1.1]

- Notarize and staple the signed macOS DMG itself so Gatekeeper can verify the
  installer before mounting it, including while offline.
- Publish Tauri v2 Linux updater manifests against the signed AppImage files.

## [0.1.0]

Initial public release.

- Signed and notarized Apple Silicon macOS DMG with a bundled Ulanzi D200
  runtime, Start at login support, and signed in-app updates.
- Linux desktop packages for x86_64 and arm64 (AppImage, deb, and rpm).
- Control panel for AI coding agents running under
  [herdr](https://github.com/herdrdev/herdr).
- Front-ends: Ulanzi D200 hardware deck, Elgato Stream Deck plugin, browser
  simulator, and a native Tauri + Svelte desktop app.
- `herdeck-bridge` — token-authenticated WebSocket bridge over herdr's socket.
- `herdeck-ctl` — CLI to list, wait on, and control agents.
- `herdeck-doctor` — setup diagnostics.
- Status panel with provider usage limits (via the CodexBar CLI), a `WAITING`
  status for panes held by background work (CI, review), bilingual EN/CS UI,
  configurable tiles, themes, profiles, macros, and notifications
  (macOS + Telegram).

[Unreleased]: https://github.com/vaclavik-xyz/herdeck/compare/v0.7.1...HEAD
[0.7.1]: https://github.com/vaclavik-xyz/herdeck/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/vaclavik-xyz/herdeck/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/vaclavik-xyz/herdeck/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/vaclavik-xyz/herdeck/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/vaclavik-xyz/herdeck/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/vaclavik-xyz/herdeck/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/vaclavik-xyz/herdeck/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/vaclavik-xyz/herdeck/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/vaclavik-xyz/herdeck/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/vaclavik-xyz/herdeck/releases/tag/v0.1.0
