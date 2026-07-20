# Agent setup runbook

This is the source of truth for a coding agent that is asked to configure
Herdeck for a user. The user should not have to fight the connection UI, edit
TOML, move tokens, or infer how local Herdr sessions and remote bridges fit
together.

The agent owns the complete setup loop:

1. inventory the current machine and every remote host in scope;
2. agree on the intended topology if the request is ambiguous;
3. preserve existing configuration and unrelated settings;
4. configure Herdr, bridge processes, tokens, and Herdeck;
5. verify the real runtime, not only file syntax;
6. leave rollback material and report the active server IDs.

Do not claim completion after merely writing `config.toml`.

## Mental model

Herdeck combines zero or more local Herdr sessions with zero or more remote
Herdeck bridges:

```text
local Herdr socket ── embedded loopback bridge ─┐
local named socket ─ embedded loopback bridge ──┼─ Herdeck live source ─ deck
remote socket ─ herdeck-bridge ─ Tailscale WS ──┘
```

- A local session needs no persistent bridge service and no token. The desktop
  sidecar starts one loopback-only embedded bridge per selected socket.
- A remote session needs one `herdeck-bridge` process, one unique reachable
  port, and a bearer token.
- Tailscale SSH may be used by the setup agent to administer a remote host. It
  is not the Herdeck data path. Runtime traffic goes directly to
  `ws://<tailscale-ip>:<port>`.
- Each source has a stable server ID. The default or custom local socket is
  `local`; a named local session `review` is `local:review`; remote IDs are the
  `[[servers]].id` values from `config.toml`.
- A remote bridge's `HERDECK_SERVER_ID` is its self-reported label. The
  connector re-scopes inbound state to the matching `[[servers]].id`, which is
  the authoritative routing ID shown by Herdeck.
- Pane identity is scoped by server ID. A command is returned to the bridge
  that supplied that pane.

## Safety rules

These are requirements, not suggestions:

- Never put a bearer-token value in `config.toml`, `local.toml`, shell history,
  a command-line argument, a commit, logs, or the final report.
- On the bridge host, prefer a mode-`0600` token file. On the deck host, store
  the corresponding client secret in the OS keychain under service `herdeck`
  and account equal to the configured `token_env`.
- Never print or `cat` a token into agent output. Transfer it through a pipe or
  another non-echoing channel.
- Bind a remote bridge only to a loopback or Tailscale address. Never use
  `0.0.0.0`, a public address, or an untrusted LAN address.
- Do not create DNS records, Tailscale Funnel/Serve rules, Cloudflare tunnels,
  firewall rules, or public proxies unless the user explicitly requests that
  exact infrastructure change.
- Preserve unknown TOML keys, profiles, comments where practical, and unrelated
  services. Take a timestamped backup before changing an existing file.
- Do not overwrite an existing keychain secret unless the task explicitly
  replaces that connection. Prefer a new unique server ID and `token_env`.
- Keep server IDs, ports, launchd/systemd unit names, and token files unique.
- Do not stop or delete unrelated Herdr sessions.

## Paths and precedence

Default paths:

| Purpose | Path |
| --- | --- |
| Shareable Herdeck config | `~/.config/herdeck/config.toml` |
| Device-local Herdeck config | `~/.config/herdeck/local.toml` |
| Onboarding mode marker | `~/.config/herdeck/onboarding.toml` |
| Default Herdr socket | `~/.config/herdr/herdr.sock` |
| Named Herdr socket | `~/.config/herdr/sessions/<name>/herdr.sock` |
| Default bridge token file | `~/.config/herdeck/bridge-token` |

Relevant overrides:

1. `HERDECK_CONFIG` changes the shareable config path.
2. `HERDECK_LOCAL_CONFIG` changes the device-local config path. Otherwise
   `local.toml` lives next to the selected `config.toml`.
3. `HERDECK_PROFILE` locks the active profile for that process.
4. `HERDECK_MOCK` forces mock mode and wins over every live configuration.
5. For legacy single-socket selection, `HERDR_SOCKET` or
   `HERDR_SOCKET_PATH` wins over `HERDR_SESSION`, `[local].herdr_socket`, and
   the default socket.
6. For a remote token, the environment variable named by `token_env` wins over
   the OS-keychain value.

An explicit `HERDR_SOCKET` is an exact process-level override and suppresses
normal named-session discovery. Remove it from the desktop environment when
configuring multiple conventional local sessions.

`onboarding.toml` contains `choice = "local"` or `choice = "demo"`. Either
choice overrides remote auto-selection. When the user asks the agent for a live
remote or mixed setup, back up and remove this marker only after the live
configuration and secrets are ready. For local-only setup, use
`choice = "local"`. Do not silently override a deliberate choice during
unrelated maintenance.

## Phase 1: inventory

Run these locally before changing anything:

```bash
command -v herdr herdeck herdeck-doctor herdeck-ctl
herdr --version
<herdeck-python> -c \
  'import importlib.metadata; print(importlib.metadata.version("herdeck"))'
herdr status --json
herdr session list --json
tailscale status --json
```

Required Herdr version is `0.7.2` or newer. `herdr status --json` checks the
default session; run `herdr --session <name> status --json` for each named
session. Every selected live session must report a running,
protocol-compatible server. `herdr session list --json` is the authoritative
list of conventional session names and socket paths.

For each remote host:

```bash
ssh <host> 'herdr --version && herdr status --json && herdr session list --json'
ssh <host> 'tailscale ip -4'
```

Read the JSON. Do not scrape human-formatted columns. Record, without secrets:

- local and remote host;
- Herdr session name;
- socket path;
- running/stopped state;
- Tailscale IPv4 address;
- intended Herdeck server ID;
- intended bridge port.

If a requested named session does not exist, ask whether to create it. Creating
or attaching it interactively is:

```bash
herdr --session <name>
```

This opens the Herdr client and is not a background provisioning command. Run
it in a user-visible terminal/session, or arrange an explicitly requested
headless Herdr service. Do not launch it in a blocking automation shell and do
not create sessions speculatively.

## Phase 2A: local sessions

Use local mode when Herdr and the Herdeck desktop sidecar run under the same
user account on the same machine.

Write only the selected session names to the `[local]` table:

```toml
[local]
herdr_sessions = ["default", "review"]
```

Use [`local.example.toml`](../local.example.toml) as a shape reference. Merge
the key into the existing file; do not replace unrelated local hardware or
profile settings.

Rules:

- `default` maps to `~/.config/herdr/herdr.sock` and server ID `local`.
- A name such as `review` maps to
  `~/.config/herdr/sessions/review/herdr.sock` and server ID `local:review`.
- A selected stopped session remains remembered and will reconnect when its
  socket appears.
- `herdr_sessions = []` explicitly selects no local session.
- Without `herdr_sessions`, Herdeck keeps legacy single-socket precedence.
- `[local].herdr_socket` is for one non-standard/custom socket. Prefer named
  Herdr sessions over custom paths when several sessions are needed.

For local-only live mode, ensure `onboarding.toml` contains:

```toml
choice = "local"
```

No `[[servers]]`, bridge token, Tailscale route, or SSH tunnel is required.
If remote entries remain in `config.toml`, `choice = "local"` intentionally
keeps them inactive.

## Phase 2B: one remote session

Use one persistent `herdeck-bridge` process on the remote Herdr host. The
bridge must read the exact session socket and bind to that host's Tailscale IP.

On macOS, the supported one-instance installer is:

```bash
TAILSCALE_IP="$(tailscale ip -4 | head -n 1)"
herdeck-service install bridge \
  --system \
  --bind "$TAILSCALE_IP" \
  --port 8788 \
  --socket "$HOME/.config/herdr/herdr.sock" \
  --server-id workbox \
  --token-file "$HOME/.config/herdeck/bridge-token"
herdeck-service status bridge --system
```

`--system` installs a root-owned plist in `/Library/LaunchDaemons`, while the
bridge process itself runs as the invoking user. The command requests macOS
administrator approval only for launchd installation/removal; token values are
never passed to `sudo`. It removes a legacy user/GUI bridge only after the new
daemon has bootstrapped and passed `launchctl print`. If migration fails, it
restores the previous service. The daemon's `KeepAlive` and restart throttle let
it recover when Tailscale or the Herdr socket becomes available after boot.

Without `--system`, `herdeck-service` retains the background LaunchAgent mode
for development or login-scoped services. Do not use that mode for an always-on
remote bridge.

The installer creates the token file when absent and rejects permissive token
file modes. For Linux, copy
[`deploy/herdeck-bridge.service`](../deploy/herdeck-bridge.service) to
`~/.config/systemd/user/herdeck-bridge.service`, replace its placeholder
address and paths, securely create the token file named by
`HERDECK_TOKEN_FILE`, then enable the user unit:

```bash
install -d -m 700 "$HOME/.config/herdeck"
umask 077
test -s "$HOME/.config/herdeck/bridge-token" ||
  openssl rand -hex 32 > "$HOME/.config/herdeck/bridge-token"
chmod 600 "$HOME/.config/herdeck/bridge-token"
systemctl --user daemon-reload
systemctl --user enable --now herdeck-bridge.service
systemctl --user status herdeck-bridge.service --no-pager
```

Configure the deck host:

```toml
[[servers]]
id = "workbox"
url = "ws://100.x.y.z:8788"
token_env = "HERDECK_WORKBOX_TOKEN"
```

IDs must be unique. `token_env` names share one flat namespace across all
servers, profiles, and notification backends; they must also be unique.

Transfer the remote token into the deck host's keychain without printing it:

```bash
ssh <host> 'cat ~/.config/herdeck/bridge-token' |
  <herdeck-python> -c '
import sys
import keyring
value = sys.stdin.read().strip()
if not value:
    raise SystemExit("empty bridge token")
keyring.set_password("herdeck", "HERDECK_WORKBOX_TOKEN", value)
'
```

`<herdeck-python>` must be the interpreter/environment used by Herdeck, for
example `/path/to/herdeck/.venv/bin/python`. The token travels through stdin;
the command prints nothing. If an environment variable named
`HERDECK_WORKBOX_TOKEN` is already exported, it takes precedence over the
keychain. Do not overwrite or unset it without understanding who owns it.

## Phase 2C: several remote sessions

Each remote Herdr session needs its own bridge process:

| Session | Socket | Port | Suggested ID | Token file |
| --- | --- | --- | --- | --- |
| default | `~/.config/herdr/herdr.sock` | `8788` | `workbox` | `bridge-workbox.token` |
| review | `~/.config/herdr/sessions/review/herdr.sock` | `8789` | `workbox-review` | `bridge-workbox-review.token` |

The resulting deck-host config is:

```toml
[[servers]]
id = "workbox"
url = "ws://100.x.y.z:8788"
token_env = "HERDECK_WORKBOX_TOKEN"

[[servers]]
id = "workbox-review"
url = "ws://100.x.y.z:8789"
token_env = "HERDECK_WORKBOX_REVIEW_TOKEN"
```

Current limitation: `herdeck-service` uses the fixed launchd label
`dev.herdeck.bridge`, so it can install only one bridge per macOS user. For
additional bridge instances, copy
[`deploy/dev.herdeck.bridge.plist`](../deploy/dev.herdeck.bridge.plist) and
change all of these per instance:

- `Label`, for example `dev.herdeck.bridge.workbox-review`;
- `HERDR_SOCKET`;
- `HERDECK_PORT`;
- `HERDECK_SERVER_ID`;
- `HERDECK_TOKEN_FILE`;
- log paths.

Before loading a manual unit, securely create its unique token file:

```bash
install -d -m 700 "$HOME/.config/herdeck"
umask 077
test -s "$HOME/.config/herdeck/bridge-workbox-review.token" ||
  openssl rand -hex 32 > "$HOME/.config/herdeck/bridge-workbox-review.token"
chmod 600 "$HOME/.config/herdeck/bridge-workbox-review.token"
```

Install each distinct plist with:

```bash
launchctl bootstrap "user/$(id -u)" \
  "$HOME/Library/LaunchAgents/dev.herdeck.bridge.<slug>.plist"
launchctl print "user/$(id -u)/dev.herdeck.bridge.<slug>"
```

On Linux, copy the systemd unit under a unique filename per session and change
the same five environment values. A port and token file must never be shared
accidentally. Sharing one intentional token is technically possible but is not
the agent default because it expands the impact of one leaked credential.

## Phase 2D: mixed local and remote

Mixed mode is just both configurations present:

`local.toml`:

```toml
[local]
herdr_sessions = ["default", "review"]
```

`config.toml`:

```toml
[[servers]]
id = "workbox"
url = "ws://100.x.y.z:8788"
token_env = "HERDECK_WORKBOX_TOKEN"
```

The desktop sidecar creates embedded bridges for the selected local sockets and
connects directly to every remote `[[servers]]` entry. Remove either stale
onboarding choice (`local` or `demo`) when the user explicitly requested this
live mixed topology. Leaving `choice = "local"` would suppress the remote
entries.

For remote-only mode, remove the explicit `herdr_sessions` key or set it to
`[]`, according to whether legacy single-socket fallback should remain
available.

## Profiles and server visibility

Without a profile-specific server selection, all top-level `[[servers]]` are
active. A profile may restrict them:

```toml
[profiles.work]
servers = ["workbox", "workbox-review"]
```

When adding a remote server, inspect the active profile and every inherited
`servers` list. Add the new ID where the user's requested topology requires it.
A valid top-level `[[servers]]` entry that is absent from the active profile is
not connected.

Local sessions are device-local and are not listed in profile `servers`.

## Applying changes

Before editing existing state:

```bash
CONFIG_PATH="${HERDECK_CONFIG:-$HOME/.config/herdeck/config.toml}"
if [ -n "${HERDECK_LOCAL_CONFIG:-}" ]; then
  LOCAL_PATH="$HERDECK_LOCAL_CONFIG"
else
  LOCAL_PATH="$(dirname "$CONFIG_PATH")/local.toml"
fi
ONBOARDING_PATH="$(dirname "$CONFIG_PATH")/onboarding.toml"

STAMP="$(date +%Y%m%d-%H%M%S)"
for path in "$CONFIG_PATH" "$LOCAL_PATH" "$ONBOARDING_PATH"; do
  if [ -e "$path" ]; then
    cp -p "$path" "$path.$STAMP.bak"
  fi
done
```

Use these resolved variables consistently for inventory, edits, verification,
and rollback. `onboarding.toml` always lives beside the effective
`config.toml`, even when `HERDECK_LOCAL_CONFIG` points elsewhere.

Use an atomic temp-file-and-rename write or the agent's structured patch tool.
Never truncate an existing TOML file and reconstruct only the keys understood
by this runbook.

The running desktop sidecar watches `config.toml`, `local.toml`, and selected
socket availability. File changes normally reconnect in place. Restart Herdeck
when changing process environment variables such as `HERDECK_CONFIG`,
`HERDECK_LOCAL_CONFIG`, `HERDECK_MOCK`, `HERDR_SOCKET`, or a token environment
variable.

## Automation surface

For unattended setup, prefer the documented CLI, `config.toml`,
`local.toml`, keychain, and service manager flows above. The desktop's
`GET /setup` and `POST /setup/connect` routes are an internal loopback API
protected by an ephemeral sidecar token that Tauri deliberately keeps out of
the WebView. Do not scrape a running desktop process for that token.

When an agent itself starts `python -m herdeck.deckapp`, it owns the discovery
line and may use the loopback API for a temporary diagnostic. Do not persist or
report its access token. The durable source of truth remains the TOML/keychain
state, not the temporary sidecar.

## Phase 3: verification

Verification is mandatory and should be proportional to the topology.

### 1. Verify every selected local Herdr session

```bash
herdr session list --json
herdr --session default api snapshot |
  jq -e '.result.snapshot.agents | type == "array"'
herdr --session review api snapshot |
  jq -e '.result.snapshot.agents | type == "array"'
```

Run the snapshot command for each selected running session. A deliberately
selected stopped session may remain unavailable, but report it as remembered
and offline rather than connected.

### 2. Validate remote configuration and bridges

```bash
HERDECK_CONFIG="$CONFIG_PATH" \
HERDECK_LOCAL_CONFIG="$LOCAL_PATH" \
  herdeck-doctor
```

`herdeck-doctor` parses the effective config, resolves env/keychain secrets,
and contacts every configured remote bridge. Treat a failed server check as a
failed setup. In local-only mode, doctor checks the legacy socket, not the full
multi-session fleet; use the Herdr snapshot checks above for local sessions.

### 3. Smoke-test the exact desktop sidecar topology

Run this from the installed Herdeck environment. It starts a temporary
loopback-only sidecar, prints only non-secret health state, and terminates it:

```bash
<herdeck-python> - <<'PY'
import json
import os
import subprocess
import sys
import time
import urllib.request

proc = subprocess.Popen(
    [sys.executable, "-m", "herdeck.deckapp"],
    stdout=subprocess.PIPE,
    text=True,
    env={**os.environ, "HERDECK_DECKAPP_PORT": "0"},
)
try:
    line = proc.stdout.readline()
    info = json.loads(line)
    health = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with urllib.request.urlopen(
            f"{info['url']}/health?token={info['token']}", timeout=2
        ) as response:
            health = json.load(response)
        connections = health.get("connections", {})
        if health.get("source") == "live" and connections and all(connections.values()):
            break
        time.sleep(0.25)
    safe = {
        key: health.get(key)
        for key in ("ok", "source", "connected", "server_ids", "connections")
    }
    print(json.dumps(safe, sort_keys=True))
    if health.get("source") != "live":
        raise SystemExit("sidecar did not select live mode")
    failed = [name for name, ok in health.get("connections", {}).items() if not ok]
    if failed:
        raise SystemExit("disconnected servers: " + ", ".join(failed))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
PY
```

Expected IDs are `local`, `local:<name>`, and the configured remote IDs. The
loopback access token is consumed inside the script and is never printed.

### 4. Verify agent routing when agents exist

After the real runtime is active:

```bash
herdeck-ctl --json ls
herdeck-ctl --json --server workbox ls
```

Use this to confirm agent rows and remote server filtering. Do not use
`herdeck-ctl` alone as proof of a multi-local desktop fleet: the CLI's local
fallback connects one legacy socket, while the desktop sidecar owns local
multi-session aggregation.

## Troubleshooting

| Symptom | Check | Corrective action |
| --- | --- | --- |
| Herdeck stays in mock mode | `HERDECK_MOCK` | Remove it from the owning service/environment and restart, only if live mode was requested. |
| Herdeck stays in demo mode | `onboarding.toml` | Back it up and remove it after a requested live remote/mixed setup is ready. |
| Named sessions are not discovered | `HERDR_SOCKET` / `HERDR_SOCKET_PATH` | Remove the exact-socket override from the desktop environment for normal fleet discovery. |
| A selected local session is offline | `herdr session list --json` and socket path | Start that Herdr session; Herdeck keeps the selection and reconnects automatically. |
| Remote config is ignored | active profile `servers` | Add the remote ID to the active/inherited profile selection. |
| Token is reported missing | `token_env`, environment, keychain account | Ensure the keychain account exactly matches `token_env`; remember env wins. |
| Token is rejected | bridge token file versus client secret | Transfer the current token again without printing it, then restart only the affected connection. |
| Remote host is unreachable | Tailscale online state, IP, bridge port | Restore Tailscale/bridge service. For an always-on macOS host, verify `herdeck-service status bridge --system`. Do not add a public proxy as a shortcut. |
| Second macOS bridge replaces the first | launchd label | Use a distinct manually installed plist; `herdeck-service` supports one bridge label. |
| Two bridge processes cannot start | duplicate port | Assign a unique port and update the matching `[[servers]].url`. |
| Config parses but one server is absent | duplicate ID or profile filtering | Make IDs unique and inspect active profile inheritance. |
| Commands reach the wrong session | server IDs/config | Verify the runtime `connections` map and target `server:pane_id`; never target by pane ID alone. |

## Rollback

If verification fails:

1. restore the timestamped TOML and onboarding backups atomically;
2. reload or restart the affected Herdeck runtime;
3. stop only bridge units created by this task;
4. remove only newly created keychain accounts or token files;
5. verify the restored topology.

Do not delete an old token/keychain entry until the new topology has passed its
runtime smoke test.

## Completion report

Report concise, non-secret evidence:

- Herdr and Herdeck versions;
- local session names and whether each is connected or remembered/offline;
- remote server IDs, Tailscale IPs, and ports;
- bridge service/unit names and status;
- config and local-config paths;
- active profile and its selected remote IDs;
- `herdeck-doctor` result;
- sidecar `source`, `server_ids`, and `connections` result;
- backup paths and any known limitation.

Never include token values or the sidecar loopback access token.
