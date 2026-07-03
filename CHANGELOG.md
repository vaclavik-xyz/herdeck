# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
