# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/vaclavik-xyz/herdeck/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/vaclavik-xyz/herdeck/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/vaclavik-xyz/herdeck/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/vaclavik-xyz/herdeck/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/vaclavik-xyz/herdeck/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/vaclavik-xyz/herdeck/releases/tag/v0.1.0
