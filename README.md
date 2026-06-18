# Herdeck

Turn an Ulanzi Stream Controller D200 into a control panel for AI coding agents
running under [herdr](https://github.com/ogulcancelik/herdr) on remote servers.
See blocked agents at a glance and Approve / Deny / Stop with one press.

## Architecture
- `herdeck-bridge` runs on each server: connects to herdr's local Unix socket,
  maps/filters panes to agents, and exposes an authenticated WebSocket bound to
  the Tailscale interface only. It pushes live status changes (poll + diff) so
  the deck updates without manual refresh. **No SSH tunnel is in the data path.**
- The Mac app connects over Tailscale and drives the deck. It resyncs fully on
  every reconnect, so sleeping and waking the Mac needs no manual steps.

```
agents → herdr (Unix socket) → herdeck-bridge → WebSocket/Tailscale → Mac app → D200
```

## Server setup (where herdr runs)
herdr's socket lives at `~/.config/herdr/herdr.sock` (macOS & Linux).

1. Copy this repo to the server and create a venv (Python ≥ 3.12):
   `python3 -m venv ~/herdeck/.venv && ~/herdeck/.venv/bin/pip install websockets`
2. Run the bridge bound to the host's **Tailscale IP** with a bearer token:
   ```bash
   HERDR_SOCKET=~/.config/herdr/herdr.sock \
   HERDECK_BIND=100.x.y.z HERDECK_PORT=8788 \
   HERDECK_SERVER_ID=workbox HERDECK_TOKEN=<random-token> \
   PYTHONPATH=~/herdeck/src ~/herdeck/.venv/bin/python -m herdeck.bridge
   ```
   The Mac routes commands by the **config `id`** of the server it connects to,
   so `HERDECK_SERVER_ID` is only a cosmetic label — the connector re-stamps
   inbound state to the config id (they need not match).
3. To keep it running: **macOS** → `deploy/dev.herdeck.bridge.plist`
   (`launchctl load -w ~/Library/LaunchAgents/dev.herdeck.bridge.plist`);
   **Linux** → `deploy/herdeck-bridge.service` (systemd).

## Mac setup (where the deck is)
1. `pip install -e ".[deck]"` (pulls `strmdck` + `pillow`).
2. Copy `config.example.toml` to `~/.config/herdeck/config.toml`, set the
   server URL to `ws://<server-tailscale-ip>:8788` and export the token env
   (e.g. `export HERDECK_DEV_TOKEN=<token>`).
3. Close the official Ulanzi app (it holds the USB device).
4. Run: `HERDECK_CONFIG=~/.config/herdeck/config.toml python -m herdeck.app`
   (or load `deploy/com.herdeck.app.plist` to autostart at login).

## Development without hardware
`HERDECK_FAKE_DECK=1 HERDECK_CONFIG=~/.config/herdeck/config.toml python -m herdeck.app`
uses an in-memory renderer — useful to verify the pipeline against a live bridge.
`scripts/e2e_verify.py` connects the real pipeline to a bridge and prints the
resulting tiles (`HERDECK_E2E_URL` / `HERDECK_E2E_TOKEN`).

## The deck (Ulanzi D200)
The D200 has **13 buttons** (a 5×3 grid minus the small status window). The
orchestrator takes the real button count from the driver: agent tiles fill the
first N−3 slots; the last three are **Next** (jump to next blocked), **Refresh**,
and **Link** (connection status). State is encoded by color: working = green,
idle = blue, blocked = amber, done = dim, error/disconnected = red.

## Adding an agent type
Add an `[answer_profiles.<name>]` block with `approve`/`deny`/`stop` (and
optional `approve_always`) key lists. `<name>` matches herdr's detected `agent`
(e.g. `claude`, `codex`); unknown agents use `[answer_profiles.default]`.

## Security
- The bridge WebSocket is authenticated with a bearer token (constant-time
  compare) and must be bound to the Tailscale interface only (`HERDECK_BIND`),
  never `0.0.0.0`. Non-idempotent key sends are never retried (no double-approve).
- The token is read from an environment variable; never commit it. The example
  launchd/systemd units store it inline — for real use keep them readable only by
  your user (`chmod 600`) or source the token from a secret store / Keychain.

## Known bring-up items
- The `strmdck` calls in `driver/d200.py` and the cwd→label derivation are
  written against the real library/herdr protocol but need a physically attached
  D200 to verify rendering and key input.
- Drill-in shows the read prompt text on a spare tile; richer prompt display is
  future work.
