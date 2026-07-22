# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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

### Fixed
- Herdr 0.7.5 protocol compatibility for submitted prompts and agent launches,
  while preserving protocol 16 and custom argv-based start profiles.

## [0.1.0]

Initial public release.

- Control panel for AI coding agents running under
  [herdr](https://github.com/ogulcancelik/herdr).
- Front-ends: Ulanzi D200 hardware deck, Elgato Stream Deck plugin, browser
  simulator, and a native Tauri + Svelte desktop app.
- `herdeck-bridge` — token-authenticated WebSocket bridge over herdr's socket.
- `herdeck-ctl` — CLI to list, wait on, and control agents.
- `herdeck-doctor` — setup diagnostics.
- Status panel with provider usage limits (via the CodexBar CLI), a `WAITING`
  status for panes held by background work (CI, review), bilingual EN/CS UI,
  configurable tiles, themes, profiles, macros, and notifications
  (macOS + Telegram).

[Unreleased]: https://github.com/vaclavik-xyz/herdeck/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/vaclavik-xyz/herdeck/releases/tag/v0.1.0
