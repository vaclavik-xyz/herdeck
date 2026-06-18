# Herdeck

Turn an Ulanzi D200 Stream Deck into a control panel for AI coding agents
running under [herdr](https://github.com/ogulcancelik/herdr) on remote servers.
See blocked agents at a glance and Approve / Deny / Stop with one press.

## Architecture
- `herdeck-bridge` runs on each server: talks to herdr's local Unix socket,
  exposes an authenticated WebSocket bound to the Tailscale interface only.
- The Mac app connects over Tailscale and drives the deck. It resyncs fully on
  every reconnect, so sleeping and waking the Mac needs no manual steps.

## Setup
1. Server: install, set env in `deploy/herdeck-bridge.service`, enable the unit.
2. Mac: `pip install -e ".[deck]"`, copy `config.example.toml`, set tokens via
   env, load `deploy/com.herdeck.app.plist`.
3. Close the official Ulanzi app (it holds the USB device).

## Development without hardware
`HERDECK_FAKE_DECK=1 python -m herdeck.app` uses an in-memory renderer.

## Adding an agent type
Add an `[answer_profiles.<name>]` block with `approve`/`deny`/`stop` key lists.

## Security
- The bridge WebSocket is authenticated with a bearer token and should be bound
  to the Tailscale interface only (`HERDECK_BIND`), never `0.0.0.0`.
- The token is read from an environment variable; never commit it. Note the
  example `deploy/com.herdeck.app.plist` stores it inline — for real use, keep
  the plist readable only by your user (`chmod 600`) or source the token from a
  secret store / Keychain instead.
