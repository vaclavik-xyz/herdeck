# Herdeck

Turn an Ulanzi Stream Controller D200 into a control panel for AI coding agents
running under [herdr](https://github.com/ogulcancelik/herdr) on remote servers.
See blocked agents at a glance and Approve / Deny / Stop with one press.

## Quick start (local)

If herdr runs on the same machine as your deck, no config or token is needed:

1. `pip install -e ".[deck]"` (Mac, real D200), `pip install -e ".[elgato]"`
   (Elgato Stream Deck), or `pip install -e ".[dev]"` (web simulator only).
2. Make sure herdr is running (socket at `~/.config/herdr/herdr.sock`).
3. Run it:
   ```bash
   herdeck                  # drives an attached Stream Deck
   HERDECK_DECK=web herdeck # browser simulator at http://127.0.0.1:8800
   ```

herdeck auto-detects the local herdr socket and starts an embedded loopback
bridge for you. If no Stream Deck is attached it falls back to the web
simulator and prints its URL. Set `HERDR_SOCKET` if herdr's socket lives
elsewhere. For a remote deck (herdr on another host) see **Server setup**
below — that path uses an explicit config with `[[servers]]` and a token.

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

**Browser simulator (recommended).** `HERDECK_DECK=web` runs a pixel-faithful
deck in the browser — it renders tiles/panel with the exact device code and turns
clicks into presses. Two ways to use it:

- **Against the live bridge** (real agents, even remotely over Tailscale):
  ```bash
  HERDECK_DECK=web HERDECK_CONFIG=~/.config/herdeck/config.toml \
  HERDECK_DEV_TOKEN=<token> python -m herdeck.app
  # open http://127.0.0.1:8800  (set HERDECK_WEB_BIND to a Tailscale IP for remote)
  ```
- **Fully offline** (synthetic, lively agents — no bridge, config, or token):
  ```bash
  HERDECK_MOCK=1 HERDECK_DECK=web python -m herdeck.app
  # open http://127.0.0.1:8800
  ```

`HERDECK_WEB_PORT` (default 8800) and `HERDECK_WEB_BIND` (default 127.0.0.1)
configure the server. Click a tile to press it; click the panel to page.

**Headless.** `HERDECK_FAKE_DECK=1 python -m herdeck.app` uses an in-memory
renderer (no UI). `scripts/e2e_verify.py` connects the pipeline to a bridge and
prints the resulting tiles (`HERDECK_E2E_URL` / `HERDECK_E2E_TOKEN`).

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

## Hardware notes (verified on a real D200, macOS)
- Rendering and key input both work on macOS. The driver opens the deck's
  **control interface by path** (HID usage_page `0x0c`); opening by vendor/product
  nondeterministically grabs the keyboard interface (held by the OS) and fails.
- Quit the official **Ulanzi Studio** app first — it auto-runs and holds the
  device. Physical buttons below the screen report indices beyond the 13 tiles
  and are ignored by the orchestrator.

## Known follow-ups
- Confirm exact approve/deny key sequences per agent against live prompts
  (config-only changes).
- Drill-in shows the read prompt text on a spare tile; richer prompt display is
  future work.

## License
MIT
