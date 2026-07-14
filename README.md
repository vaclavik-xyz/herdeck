# Herdeck

![CI](https://github.com/vaclavik-xyz/herdeck/actions/workflows/ci.yml/badge.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)

Turn an Ulanzi Stream Controller D200 (or an Elgato Stream Deck) into a control
panel for AI coding agents running under
[herdr](https://github.com/ogulcancelik/herdr). See blocked agents at a glance
and Approve / Deny / Stop with one press — on the hardware deck, a browser
simulator, or a native desktop window.

> **What is herdr?** herdr runs your AI coding agents (Claude, Codex, Cursor,
> Gemini, …) in managed terminal panes and exposes their live state over a local
> socket. herdeck is a front-end for it and requires **herdr >= 0.7.2** (its
> `session.snapshot` API; check with `herdr status`, upgrade with `herdr update`).
> You install and run herdr separately; or use the mock path below to try
> herdeck standalone.

## Try it in 30 seconds (no hardware, no herdr)

```bash
git clone https://github.com/vaclavik-xyz/herdeck.git && cd herdeck
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
HERDECK_MOCK=1 HERDECK_DECK=web herdeck   # synthetic agents — no deck, bridge, config, or token
# open http://127.0.0.1:8800
```

This renders the deck in your browser with lively synthetic agents using the
exact device code — no Stream Deck and no herdr required.

## Quick start (local, real agents)

If herdr runs on the same machine as your deck, no config or token is needed:

1. `git clone https://github.com/vaclavik-xyz/herdeck.git && cd herdeck`
2. `pip install -e ".[deck]"` (Mac, real D200), `pip install -e ".[elgato]"`
   (Elgato Stream Deck), or `pip install -e ".[dev]"` (web simulator only).
3. Make sure herdr is running (socket at `~/.config/herdr/herdr.sock`).
4. Run it:
   ```bash
   herdeck                  # drives an attached Stream Deck
   HERDECK_DECK=web herdeck # browser simulator at http://127.0.0.1:8800
   ```

herdeck auto-detects the local herdr socket and starts an embedded loopback
bridge for you. If no Stream Deck is attached it falls back to the web
simulator and prints its URL. Set `HERDR_SOCKET` (Herdeck compatibility) or
Herdr's native `HERDR_SOCKET_PATH` / `HERDR_SESSION` if the socket lives
elsewhere. For a remote deck (herdr on another host) see **Server setup**
below — that path uses an explicit config with `[[servers]]` and a token.

Run `herdeck-doctor` to diagnose setup problems — it checks the herdr socket,
config/mode, deck availability, and (for remote) token presence, printing a
pass/fail checklist with hints (it never prints token values).

## Controlling agents from the CLI (`herdeck-ctl`)

`herdeck-ctl` drives agents from a terminal — for scripting or for a lead agent
orchestrating others — using the same bridge and answer profiles as the deck.

```bash
herdeck-ctl ls --json                          # list agents + status
herdeck-ctl wait --any --until blocked --json  # block until one needs input
herdeck-ctl approve local:w1:p1                # approve a blocked agent
herdeck-ctl focus local:w1:p1                  # bring its pane to the foreground
herdeck-ctl send local:w1:p1 "run the tests"   # send text (submits immediately)
```

Target an agent by `server:pane_id` or a fuzzy match on its label/repo/branch.
Common options (`--json`, `--server`, `--config`, `--timeout`) work before or
after the subcommand. `wait` is the one exception: its own `--timeout` (max
seconds to wait, default: no limit) goes after it, e.g.
`wait --any --until blocked --timeout 60`.

Exit codes: `0` ok · `2` usage · `3` skipped (agent not blocked) · `4`
unknown/ambiguous agent · `5` connection/config error · `124` `wait` timed out.
Actions that clear a block (`approve`/`deny`/`stop`) wait until the agent leaves
`blocked` before returning (tune with `--settle S` / `--no-settle`).

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

For a service installation, prefer a private token file over an environment
value:

```bash
mkdir -p ~/.config/herdeck
openssl rand -hex 32 > ~/.config/herdeck/bridge-token
chmod 600 ~/.config/herdeck/bridge-token
HERDECK_TOKEN_FILE=~/.config/herdeck/bridge-token \
  HERDECK_BIND=100.x.y.z python -m herdeck.bridge
```

## Mac setup (where the deck is)
1. `pip install -e ".[deck]"` (pulls `strmdck` + `pillow`).
2. Copy `config.example.toml` to `~/.config/herdeck/config.toml`, set the
   server URL to `ws://<server-tailscale-ip>:8788` and export the token env
   named by the server's `token_env` (the shipped example uses
   `export HERDECK_WORKBOX_TOKEN=<token>`).
3. Close the official Ulanzi app (it holds the USB device).
4. Run: `HERDECK_CONFIG=~/.config/herdeck/config.toml python -m herdeck.app`
   (or load `deploy/com.herdeck.app.plist` to autostart at login).

## Profiles and customization

Herdeck supports a shareable `config.toml` and a device-local `local.toml`.
The shareable file defines profiles and reusable blocks for theme, view,
launcher, macros, notifications, and safety. The local file stores the active
profile and device-specific settings such as deck type, socket path, web bind,
icon overrides, and hardware tuning.

Switch profiles from the deck through `+ New` -> `Profiles`, or set
`HERDECK_PROFILE=mobile` to lock a process to a profile. Use `local.toml` for
values you do not want to share between devices:

```toml
active_profile = "mobile"

[local]
deck = "web"
herdr_socket = "~/.config/herdr/herdr.sock"
web_bind = "127.0.0.1"
web_port = 8800
icons_dir = "~/herdeck-icons"

[hardware]
brightness = 80
debounce = 0.25
keep_alive_interval = 5.0
tick_interval = 0.4
```

## Development without hardware

**Browser simulator (recommended).** `HERDECK_DECK=web` runs a pixel-faithful
deck in the browser — it renders tiles/panel with the exact device code and turns
clicks into presses. Two ways to use it:

- **Against the live bridge** (real agents, even remotely over Tailscale):
  ```bash
  HERDECK_DECK=web HERDECK_CONFIG=~/.config/herdeck/config.toml \
  HERDECK_WORKBOX_TOKEN=<token> python -m herdeck.app
  # open http://127.0.0.1:8800  (set HERDECK_WEB_BIND to a Tailscale IP for remote)
  ```
- **Fully offline** (synthetic, lively agents — no bridge, config, or token):
  ```bash
  HERDECK_MOCK=1 HERDECK_DECK=web python -m herdeck.app
  # open http://127.0.0.1:8800
  ```

`HERDECK_WEB_PORT` (default 8800) and `HERDECK_WEB_BIND` (default 127.0.0.1)
configure the server. Click a tile to press it; click the panel to page.
Long-press or right-click an agent tile (or focus it and press `Shift+Enter`) to
open a live read-only terminal preview; close it with `Esc` or ×. The bridge
host needs herdr >= 0.7.3 for `herdr terminal session observe`.

The simulator URL token now grants both deck controls and visibility into agent
terminals. Treat it as a credential and bind the simulator only to loopback or
your trusted Tailscale interface — never `0.0.0.0`, a public IP, or an untrusted
LAN.

### Running as a service

`herdeck-web` makes the browser runtime explicit, and `herdeck-service` installs
macOS LaunchAgents without placing token values in plist files:

```bash
herdeck-service install bridge --bind 100.x.y.z --server-id workbox
herdeck-service install web --bind 100.x.y.z --config ~/.config/herdeck/config.toml
herdeck-service status web
```

The install command creates the bridge token at
`~/.config/herdeck/bridge-token` with mode `0600` when needed. Web capability
URLs are disabled by default and are no longer written to normal startup logs.
`herdeck-web url --allow-query-token` prints one only when the server was also
started with that explicit legacy opt-in. Do not enable it for a persOS cockpit.

`GET /healthz` is an unauthenticated, non-sensitive liveness response. The
token-protected `GET /readyz` also reports readiness when called with
`X-Herdeck-Token`. Probe a running
web service without exposing its token with:

```bash
herdeck-doctor --web-url http://100.x.y.z:8800
```

### Reverse proxy and cockpit embed

Herdeck can run behind an HTTPS reverse proxy under a preserved path prefix.
Keep the backend on loopback, forward `/herdeck/` without stripping the prefix,
and opt in only the exact HTTPS origin allowed to frame the deck:

```bash
herdeck-service install web \
  --bind 127.0.0.1 \
  --config ~/.config/herdeck/config.toml \
  --base-path /herdeck \
  --public-origin https://cockpit.example \
  --frame-ancestor https://cockpit.example

herdeck-doctor --web-url https://cockpit.example/herdeck/
```

By default the Content Security Policy uses `frame-ancestors 'none'`. Frame
ancestors must be explicit HTTPS origins; wildcards, paths, and forwarded trust
headers are not accepted. `HERDECK_WEB_FRAME_ANCESTORS` is the comma-separated
environment equivalent of repeated `--frame-ancestor` options.

An embed policy requires an explicit HTTPS `public_origin`. Hosting Herdeck
under the cockpit's own origin (as in the `/herdeck` example) is the most robust
setup and keeps `SameSite=Strict`. If an allowed parent has a different origin,
Herdeck emits `SameSite=None; Secure`; that deployment also depends on the
browser allowing third-party cookies for the iframe.

The persistent `?token=...` browser bootstrap is disabled by default. It is
available only with the explicit `--allow-query-token` legacy opt-in. A persOS
cockpit instead mints a bounded `HttpOnly; SameSite=Strict` browser session
through its authenticated server-side handoff. Browser writes additionally
require an exact `Origin` match. Existing automation continues to use
`X-Herdeck-Token`. When
`HERDECK_WEB_PUBLIC_ORIGIN` is HTTPS the session cookie is also `Secure`.

For a persOS cockpit, including the semantic agent API, server-to-server browser
session handoff, guarded approve/deny/stop/send-text calls, and the deployment
contract, see [docs/persos-cockpit.md](docs/persos-cockpit.md).

### Optional work/run context

Orchestrators can attach display-only work identity to a Herdr pane through
`pane.report_metadata` state labels. Herdeck recognizes only these bounded keys:

```text
work.source = github
work.item   = vaclavik-xyz/herdeck#123
work.run    = run-42
work.url    = https://github.com/vaclavik-xyz/herdeck/issues/123
```

Unknown labels never cross the Herdeck bridge. `work.url` must be HTTPS and is
display metadata only; Herdeck never opens or executes it automatically. Add
`source`, `work_item`, or `run` to `view.tile_primary` / `tile_secondary` to
render the context. Interactive Telegram alerts include `work.item` when set.

**Headless.** `HERDECK_FAKE_DECK=1 python -m herdeck.app` uses an in-memory
renderer (no UI). `scripts/e2e_verify.py` connects the pipeline to a bridge and
prints the resulting tiles (`HERDECK_E2E_URL` / `HERDECK_E2E_TOKEN`).

## Desktop app

herdeck also ships a native **desktop app** (Tauri + Svelte): a floating,
always-on-top window that renders the same deck as the hardware, plus a
first-run onboarding flow and a full settings / config editor. It attaches to a
running herdeck runtime or spawns its own sidecar. Build and run it from
`desktop/`:

```bash
# from the repo root — dev mode spawns .venv/bin/python -m herdeck.deckapp as
# the sidecar, so create that venv first (same as Quick start):
python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"

cd desktop
npm install
npm run tauri dev   # opens the floating window (needs a real desktop session)
```

See [`desktop/README.md`](desktop/README.md) for architecture and build/test
details.

## The deck (Ulanzi D200)
The D200 has **13 buttons** (a 5×3 grid minus the small status window). The
orchestrator takes the real button count from the driver: agent tiles fill the
slots up to the reserved **+ New** launcher tile. With more agents than tiles,
pressing the status window pages through them (the panel shows `· 1/2`), and a
newly blocked agent automatically pulls the overview back to the first page
where it sorts to the front. State is encoded by color: working = green,
idle = blue, blocked = amber, done = cyan, waiting = violet, error/disconnected
= red. **Waiting** is derived from a pane held by
[herdwatch](https://github.com/vaclavik-xyz/herdwatch) (or any source using
`herdr pane report-agent --custom-status`): the agent itself is done but
background work — CI, a review, a manual marker — is still pending; the tile
shows the holder's label (`CI`, `REVIEW +1`) in place of the status word and
does not animate. By default
the colour shows in the status word and a bottom accent bar; set
`[view].tile_fill` to `tint` (whole tile a darkened shade of the colour) or
`solid` (whole tile the full colour) for more at-a-glance visibility.

All rendered deck text (tile status words, the panel, the web simulator) and
the desktop app UI speak `[view].language` — `"en"` (default) or `"cs"`; the
desktop settings window offers it as the View → `language` select and switches
live.

The status window can also show **provider subscription limits**. Set
`[usage] providers = ["claude", "codex"]`. Codex is read from the authenticated
`codex app-server` account API. Claude Code exposes its 5-hour and weekly
limits to status-line commands; add this sidecar call near the top of your
existing status-line script, after it has read stdin into `$input`:

```bash
printf '%s' "$input" | herdeck-usage capture-claude >/dev/null 2>&1 || true
```

The default Claude snapshot is `~/.cache/herdeck/claude-usage.json`. Configure
`codex_path` when Codex lives elsewhere. If you change `claude_cache_path`, pass
the same path to the status-line collector, for example
`herdeck-usage capture-claude --output /path/to/claude-usage.json`.
Set `paid_only = true` to hide providers unless native account data confirms a
paid subscription. Codex uses the ChatGPT plan reported by app-server. Claude's
subscriber-only rate limits confirm Pro/Max after the first API response in a
Claude Code session; until then the state is unknown and stays hidden. Removing
a provider from `providers` disables it completely.
CodexBar remains an automatic compatibility fallback for missing providers;
set `codexbar_path = ""` to disable it. Pressing the status window holds a
detail view with reset times. Blocked and offline alerts always take priority.

## Stream Deck (Elgato) plugin backend
herdeck can also drive a native **Elgato Stream Deck** as a plugin. A thin
TypeScript shell (a separate follow-up plan) owns the deck over Elgato's SDK and
spawns this Python backend — the same herdeck core — as its "brain". Select it
with the deck kind `elgato-plugin`. Normally the TS shell sets the socket/token
(see the discovery contract below) and spawns the backend; to run it by hand:

```bash
HERDECK_DECK=elgato-plugin \
  HERDECK_ELGATO_SOCK=/tmp/herdeck-elgato.sock \
  HERDECK_ELGATO_TOKEN="$(openssl rand -hex 16)" \
  python -m herdeck.app
```

Unlike the D200/web front-ends, `elgato-plugin` does **not** use the grid
orchestrator. It is a separate front-end over the core: it maps live herdr agents
onto the keys you have placed on the deck (sticky slot leases — keys never
reflow, status drives color not order), tracks a single global selection (a lone
blocked agent auto-selects), and speaks a small JSON line protocol to the shell
over a local Unix socket.

**Discovery contract.** The shell picks a socket path and generates a one-shot
token, then hands both to the backend through the environment; the backend
creates and binds that socket and listens for the shell's connection:

- `HERDECK_ELGATO_SOCK` — path to the Unix socket the backend listens on.
- `HERDECK_ELGATO_TOKEN` — shared secret the shell sends in its `hello`; the
  backend rejects any connection whose token (constant-time compared) or protocol
  version mismatches. Both variables must be set or the backend exits.

**Action scope.** Approve/Deny are **binary only** — enabled solely when the
selected agent is blocked, online, its prompt has been read, and the prompt is a
yes/no with no numbered options. A multi-option prompt disables Approve/Deny on
the deck; press the agent's slot to focus its terminal and answer in the TUI.
Stop is always two-step (arm, then confirm within a few seconds) and sends a
forced interrupt regardless of the safety profile. Non-idempotent sends are never
retried.

This backend is fully usable and unit-tested on its own.

### Plugin shell (TypeScript)

The native plugin's TypeScript shell lives in `streamdeck/` and is built with the
`@elgato/streamdeck` SDK. It spawns and supervises the Python backend (passing the
socket path + one-shot token via `HERDECK_ELGATO_SOCK`/`HERDECK_ELGATO_TOKEN` and
`HERDECK_DECK=elgato-plugin`), forwards key presses, and renders the PNGs the
backend hands back — no logic of its own. Build it with `cd streamdeck && npm install
&& npm run build`; the bundle is `streamdeck/xyz.vaclavik.herdeck.sdPlugin/`.

### Packaging the plugin (local, unsigned, arm64)

`npm run package` builds a double-clickable `xyz.vaclavik.herdeck.streamDeckPlugin`
with a **frozen backend bundled inside**, so it installs and runs on a Mac with no
Python and no `herdeck` install. This milestone targets the local dev machine:
**arm64-only, unsigned/ad-hoc** (no code signing or notarization — Gatekeeper may
warn on other machines).

**Prereqs:** an arm64 Mac, the Python build deps (`pip install -e .[packaging]` into
your venv — PyInstaller 6 + the build-time SVG rasterizer cairosvg + the frozen
runtime deps), and the Node deps (`cd streamdeck && npm install`). cairosvg needs
libcairo present at build time (e.g. `brew install cairo`); it is **not** bundled.

**Build:**

```bash
pip install -e .[packaging]      # once, into the venv the build uses
cd streamdeck && npm run package # pre-rasterize → freeze → npm build → zip
```

`scripts/build-plugin.sh` runs four steps: pre-rasterize `src/herdeck/assets/*.svg`
→ PNG (so the frozen runtime is Pillow-only, never cairosvg); freeze the backend
with PyInstaller (onedir) into `…sdPlugin/backend/herdeck-backend/herdeck-backend`;
`npm run build` the TS shell; then package the `.sdPlugin` into a `.streamDeckPlugin`
with Elgato's `DistributionTool` if it is on `PATH`, else a plain `zip` (the format
is a zip of the bundle dir). All build outputs are gitignored.

**Install:** double-click the `.streamDeckPlugin` (or drag it onto the Stream Deck
app). The bundled backend is discovered automatically — no Python required.

**Backend discovery precedence** (`resolveHerdeckCommand`): the **PI-configured
path** → `HERDECK_BIN` → the **bundled** `backend/herdeck-backend/herdeck-backend`
(only when it exists) → `herdeck` on `PATH`. So a packaged install uses the bundled
frozen backend with zero config, while a dev checkout (no `backend/`) transparently
falls through to a venv/PATH `herdeck`, and an explicit PI path or `HERDECK_BIN`
always wins.

## Adding an agent type
Add it to the `[start_profiles]` section (base config) or override it per profile:

```toml
[start_profiles]
myagent = ["myagent", "--flag"]
```

Custom `[answer_profiles.<name>]` sections can be defined in the base config and overridden per-profile via `[profiles.<name>.answer_profiles.<type>]`; a profile can only override a type that the base defines.

## Notifications
Get notified when an agent enters the **blocked** state, so you don't have to
watch the deck. Configure notifications inline in the base config or as a profile
overlay:

```toml
[notifications]
enabled = true
backends = ["macos", "telegram"]   # run both, or just one
on = ["blocked"]
sound = true

# Only needed when "telegram" is a backend:
[notifications.telegram]
token_env = "HERDECK_TELEGRAM_TOKEN"   # bot token read from this env var
chat_id = "123456789"

# Optional: route alerts into a Telegram forum topic, e.g. a Hermes topic.
message_thread_id = 456

# Optional: enable buttons and reply-to-agent routing.
interactive = true
allowed_user_ids = [123456789]
prompt_max_chars = 1200

# Override per profile:
[profiles.work.notifications]
backends = ["macos"]
```

Legacy flat configs use the root `[notifications]` table with the same fields.

- **macOS** posts to Notification Center (osascript). **Telegram** delivers to
  your phone via the Bot API over HTTPS (stdlib only, no extra dependency) —
  useful when you drive herdeck from the phone over Tailscale.
- Telegram setup: create a bot with @BotFather, `export HERDECK_TELEGRAM_TOKEN=<token>`
  (never commit the token), and set your numeric `chat_id`. A missing token or
  chat_id makes herdeck skip telegram with a warning — other backends still fire.
- Non-interactive notifications contain only the repo/label, branch, and
  (multi-server) server id; they never include prompt text, command output, or
  tokens. When `interactive = true`, Telegram alerts include the current blocked
  prompt, Approve/Deny/Stop/Read again buttons, and reply routing. Reply to this message
  to send text to that specific agent. Herdeck accepts inbound actions only from
  `allowed_user_ids`, only in the configured `chat_id`, and only in `message_thread_id`
  when one is configured.
- Notifications fire once per blocked episode (re-arming after the agent leaves
  `blocked`) and never block the UI loop.

## Security
- The bridge WebSocket is authenticated with a bearer token (constant-time
  compare) and must be bound to the Tailscale interface only (`HERDECK_BIND`),
  never `0.0.0.0`. The transport is plain `ws://`, so that interface must be an
  encrypted overlay (Tailscale/WireGuard) — the token is both the authentication
  and the only confidentiality boundary. Never bind it to a plain LAN or public
  IP. Non-idempotent key sends are never retried (no double-approve).
- The bridge token is read from `HERDECK_TOKEN_FILE` (recommended) or the legacy
  `HERDECK_TOKEN` environment variable. Keep token files mode `0600`; generated
  launchd units contain only the file path, never the token value.

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
- The Elgato plugin ships end-to-end: Python backend, TypeScript shell, and a
  local `npm run package` that bundles a frozen backend into an installable
  `.streamDeckPlugin` (arm64, unsigned). Remaining: code signing/notarization,
  universal2/Intel, real (non-placeholder) icon art, and on-hardware verification.

## License

herdeck is released under the [MIT License](LICENSE) — Copyright (c) 2026
Filip Vaclavik.

### Credits and trademarks

The bundled agent marks (`src/herdeck/assets/*.svg`) come from
[Simple Icons](https://simpleicons.org) under CC0 1.0. The marks themselves
remain trademarks of their respective owners (Anthropic, OpenAI,
Microsoft/GitHub, Cursor, Google, OpenCode) and are bundled solely to identify
which agent a deck tile represents — no affiliation or endorsement is implied.
See [`src/herdeck/assets/ATTRIBUTION.md`](src/herdeck/assets/ATTRIBUTION.md).
