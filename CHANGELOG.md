# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
