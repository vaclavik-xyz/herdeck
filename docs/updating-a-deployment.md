# Updating a running deployment

The README covers installing Herdeck. This covers the other half: pushing a new
version to hosts that are already running one, and proving it actually landed.

Every trap below was hit for real, in the order it appears.

## What has to be updated, and where

A deployment is usually split across two hosts:

- the **bridge host** runs `herdeck-bridge` next to Herdr's Unix socket;
- the **render host** runs `herdeck.runtime`, drives the deck, and connects to
  the bridge over the network.

Changes to `src/herdeck/bridge.py` only take effect on the bridge host; changes
to rendering (`layout.py`, `orchestrator.py`, `icons.py`) only take effect on the
render host. `protocol.py` is neither: it is the shared wire module both sides
import, so a change there has to reach both hosts together.

Adding a field to the snapshot can be rolled out one host at a time, but only
in one order: **bridge first**. An old render host ignores a key it does not
know. The reverse runs new render code against an old snapshot, and
`_pane_to_state` defaults every missing field to `""` or `{}` — so the new field
renders blank, with no error and nothing in the log, until the bridge catches up.
Since 0.8.1 the mismatch is at least visible: every snapshot carries the
bridge's `herdeck_version`, the runtime's `/health` lists it per server next to
its own `version`, and both the desktop window and `herdeck-doctor` report a
bridge whose version differs from the runtime's.

Changing `protocol.py`'s encode/decode contract has no safe order at all; both
hosts move together, and `WIRE_PROTOCOL` in `protocol.py` must be bumped. A
runtime that receives a newer protocol than it knows logs a WARNING
(`bridge '<id>' speaks unsupported wire protocol …`) and reports
`protocol_supported: false` for that server in `/health`.

The desktop app is the exception: its frontend is compiled into the bundle, so
it does **not** pick up changes from a source sync. It needs a rebuild.

## Services, once per host

Every long-running piece runs under `herdeck-service`, which writes a launchd
unit on macOS or a systemd `--user` unit on Linux with `KeepAlive`/`Restart`,
logs in `~/Library/Logs/herdeck-<kind>.log` (Linux: `journalctl --user -u
herdeck-<kind>`) and token *files*, never token values:

| host | kind | label / unit | install |
|---|---|---|---|
| bridge host | `bridge` | `dev.herdeck.bridge` / `herdeck-bridge.service` | `herdeck-service install bridge --system --bind <tailscale-ip> --server-id <id>` |
| deck host, from source | `runtime` | `dev.herdeck.runtime` / `herdeck-runtime.service` | `herdeck-service install runtime --config ~/.config/herdeck/config.toml` |
| deck host, from the app | `runtime` | `dev.herdeck.runtime` | `herdeck-service install runtime --from-app [/Applications/herdeck.app]` |
| bridge host, managed release | `bridge` | `dev.herdeck.bridge` / `herdeck-bridge.service` | `herdeck-service install bridge --managed [--version X] --bind <tailscale-ip> --server-id <id>` |

The runtime unit runs in the login (`gui/<uid>`) session because it drives the
D200 and posts notifications; the desktop window finds it through
`~/.cache/herdeck/runtime.json`, not through the label. Run
`herdeck-service` from the venv the service should use: `--python` defaults to
the interpreter that runs the installer.

**`--from-app`** points the unit at the frozen runtime bundled inside the signed
desktop app (`Contents/Resources/herdeck-deckapp/herdeck-deckapp`) instead of a
Python checkout. That keeps runtime and app on one version: when the app's
updater installs a new release it restarts that unit (`launchctl kickstart -k
gui/<uid>/dev.herdeck.runtime`, logged in the app log) before relaunching
itself. The updater only touches a unit whose program lies inside its own
bundle; a unit that runs from a source checkout is never restarted by it. On a
host deployed from source, update the runtime with `deploy-host.sh` below.

**`--env KEY=VALUE`** (repeatable) adds a non-secret variable to the unit's
launch environment, so a host-specific switch survives a reinstall. Names
containing `TOKEN`, `SECRET` or `PASSWORD` are refused (tokens live in the
keychain, a `[[servers]]` `token_file` or the bridge token file, never in a
unit), as are variables the unit already sets. Prefer the config key where one exists:
`HERDECK_D200_STANDARD_WRITER=1` is `[hardware].d200_standard_writer = true` in
`local.toml`, and `HERDECK_T3_DESKTOP_READ_STATE=1` is `desktop_read_state =
true` on the T3 `[[servers]]` entry. The env vars still work as a fallback.

**`--managed`** (bridge only) runs the bridge from its own virtualenv at
`~/.local/share/herdeck/bridge-venv` instead of a checkout. The installer
creates it (with `uv` when present, else `python -m venv`), installs the herdeck
release — the wheel attached to the GitHub release (checked against the
release's `SHA256SUMS` when it has one), falling back to
`git+https://github.com/vaclavik-xyz/herdeck@vX` — checks that the venv imports
exactly that version, writes `managed.json {version, source, ...}` into the venv
and only then installs the unit. `--version` picks the release (default: the
version of the `herdeck-service` you run). The marker is what will let the
desktop app update a managed bridge in place; a checkout is never touched.

`herdeck-service restart <kind>` restarts an installed unit in place
(`launchctl kickstart -k` / `systemctl --user restart`), and `herdeck-service
status <kind> --json` prints `{kind, label, installed, unit_path, loaded,
program, from_app}` for scripts and the desktop app.

The desktop app's bundled runtime binary carries the same CLI as a `service`
subcommand, so a Mac without a Python install can run it:

```bash
/Applications/herdeck.app/Contents/Resources/herdeck-deckapp/herdeck-deckapp \
  service install runtime --from-app          # --from-app defaults to that same bundle
/Applications/herdeck.app/Contents/Resources/herdeck-deckapp/herdeck-deckapp \
  service status runtime --json
```

Run from the bundle, `install` accepts only `runtime --from-app` and `bridge
--managed` (anything else needs `--python`, which the frozen binary is not).

### Migrating a bridge from `nohup`

A bridge started by hand (`nohup herdeck-bridge &`, or `python -m
herdeck.bridge` in a tmux pane) survives neither a crash nor a reboot. Move it
under launchd without a new token:

```bash
pgrep -fl herdeck.bridge                       # note the PID and its interpreter
kill <PID>                                     # the deck reconnects on its own
~/herdeck/.venv/bin/herdeck-service install bridge --system \
  --bind "$(tailscale ip -4 | head -n 1)" --port 8788 \
  --server-id <id> --token-file ~/.config/herdeck/bridge-token
~/herdeck/.venv/bin/herdeck-service status bridge --system
```

Keep the same `--port`, `--server-id` and token file the `nohup` process used,
so the deck's `[[servers]]` entry keeps working unchanged. If the old process
got its token from `HERDECK_TOKEN`, write that value into the token file first
(mode `0600`) — read it from where you stored it, not from `ps eww`. Then check
the deck: `/health` should report `connected` again within a few seconds.

### Updating a managed bridge from the deck host

A bridge installed with `herdeck-service install bridge --managed` runs from its
own venv (`~/.local/share/herdeck/bridge-venv`) that carries a `managed.json`
marker at its root. Such a bridge updates itself on request; nothing has to be
copied to the bridge host:

```bash
# on the deck host; <id> is the [[servers]] id
URL="$(python3 -c 'import json,os;print(json.load(open(os.path.expanduser("~/.cache/herdeck/runtime.json")))["url"])')"
TOKEN="$(python3 -c 'import json,os;print(json.load(open(os.path.expanduser("~/.cache/herdeck/runtime.json")))["token"])')"
curl -s -X POST -H "X-Herdeck-Token: $TOKEN" -d '{"wait_ms": 20000}' \
  "$URL/maintenance/servers/<id>/update"
```

The bridge installs the **runtime's** version: the release wheel, checked
against the release's `SHA256SUMS`, into its venv; it checks the installed
version in a fresh interpreter, answers, and exits so launchd/systemd starts the
new version. The answer is `pending` while pip runs; poll `GET
/maintenance/servers/<id>/update?token=$TOKEN&after=<next>&wait_ms=20000` until
it says `updated` (then `/health` lists the new `bridge_version` for that
server) or `failed` (with the installer output in `output`; the old version
is still running). `not_managed` means the bridge runs from a checkout or an
editable install — migrate it with `--managed` first, or keep using
`deploy-host.sh --role bridge`. `unsupported` means the bridge predates
self-update and needs one update by hand.

The runtime never sends an update to a bridge that already runs its version or
a newer one (`current`/`newer`), so an older deck host sharing the bridge cannot
pull it back. The bridge only installs releases from 0.10.0 on (the first that
publishes the wheel and can update itself); a release without a wheel asset is
refused rather than installed unverified.

### Giving a runtime service its server tokens

A runtime unit has no shell environment, and `--env` refuses names containing
`TOKEN`, so a `token_env` that only your shell exported resolves to nothing
under launchd/systemd. Before switching to the service (`install runtime
--from-app` included), make every `[[servers]]` token resolvable without the
shell: keep it in the keychain (the desktop Connections editor writes there) or
point the server at a token file:

```bash
install -m 600 /dev/null ~/.config/herdeck/t3-token
pbpaste > ~/.config/herdeck/t3-token        # or copy it over ssh; never echo it
```

```toml
[[servers]]
id = "t3"
url = "http://127.0.0.1:3773"
backend = "t3"
token_env = "HERDECK_T3_HEADLESS_TOKEN"     # still wins when an env var is set
token_file = "~/.config/herdeck/t3-token"   # what the service reads
```

The file must be a regular `0600` file; a group- or world-readable one is
refused. `herdeck-doctor` prints where each server's token resolved
(`local=file, t3=keychain`) without the value. If a token still resolves from
nowhere, the runtime does not fall back to the demo fleet: the D200 shows
`CONFIG ERROR` and `GET /health` carries `config_error`. Creating or fixing
the file (or adding the keychain entry) recovers it without a restart.

### Migrating a hand-made runtime unit

A deck host that runs the runtime from a custom plist and launcher script
(for example label `com.herdeck.app`) moves the same way: `launchctl bootout
gui/$(id -u)/<old-label>`, move the old plist out of `~/Library/LaunchAgents`,
then `herdeck-service install runtime ...` from the venv (or `--from-app`).
Anything that restarts the runtime by label — the `t3-renew` job's
`--restart-label`, for instance — must switch to `dev.herdeck.runtime`.

## Deploying a source checkout: `scripts/deploy-host.sh`

For a runtime or bridge host that runs from source, one command ships a
committed ref, installs it, restarts the unit and proves it came back:

```bash
scripts/deploy-host.sh --role runtime --host macbench            # deck host
scripts/deploy-host.sh --role bridge  --host m4 --ref v0.9.0     # bridge host
scripts/deploy-host.sh --role runtime                            # this machine
```

What it does, in order:

1. `git archive` of `--ref` (default `HEAD`) — the committed state, never your
   working tree — unpacked on the target as `~/herdeck-deploy/releases/<sha>/`.
   A snapshot that already exists is reused, so re-running a deploy is safe.
2. Refuses to go on if the `dev.herdeck.<role>` unit runs anything other than
   the target venv (`~/herdeck-deploy/venv` by default): a `--from-app` runtime
   or another checkout would restart unchanged and pass the health check
   without running a line of what was deployed.
3. `pip install -e <snapshot>[deck]` (runtime) or `-e <snapshot>` (bridge)
   into the venv, creating it with `--python` on first use.
4. Points `current` at the new snapshot and `previous` at the one it replaces.
5. Restarts the unit (`launchctl kickstart -k`, `sudo -n` for a `--system`
   bridge, or `systemctl --user restart`).
6. Verifies. Runtime: waits for a *rewritten* `runtime.json`, then
   `GET /health` with its token must say `ok` (it also reports whether the
   bridge is connected). Bridge: its `HERDECK_BIND:HERDECK_PORT` from the unit
   (or `--bridge-addr`) must accept TCP connections.
7. Prunes old snapshots, keeping `current`, `previous` and `--keep` more.

A failed health check exits 1, points at the log, and prints the command that
undoes the deploy:

```bash
scripts/deploy-host.sh --role runtime --root herdeck-deploy --rollback --host macbench
```

`--rollback` re-installs `previous` into the venv, swaps the symlinks and runs
the same restart and health check. The first deploy to a host stops with exit
code 3 after installing, printing the one `herdeck-service install` command
still missing; re-run the deploy after it. `--help` lists every flag.

Snapshots are immutable directories, so none of the traps of syncing into a
live tree apply: removed modules really disappear, a dropped connection leaves
the running release untouched, and the venv lives outside the snapshot.

**Still not covered by any source sync: the desktop app.** Its frontend is
compiled into the bundle; see *Rebuilding the desktop app*. The app's own
updater refreshes an app-bundled runtime (`--from-app`) with it.

### Manual fallback

Without the script — for a host with an older, hand-laid-out checkout — sync
into a fresh directory and swap it in, rather than extracting over the live
tree (`tar -x` never deletes, so a removed module would stay importable):

```bash
git archive --format=tar main | ssh HOST 'rm -rf ~/herdeck.new && mkdir ~/herdeck.new && tar -x -C ~/herdeck.new'
ssh HOST 'test -d ~/herdeck.new/src/herdeck && rm -rf ~/herdeck.old \
  && mv ~/herdeck/src/herdeck ~/herdeck.old && mv ~/herdeck.new/src/herdeck ~/herdeck/src/herdeck'
ssh HOST '~/herdeck/.venv/bin/pip install -e "$HOME/herdeck[deck]"'
ssh HOST 'launchctl kickstart -k gui/$(id -u)/dev.herdeck.runtime'
```

Roll back with `test -d ~/herdeck.old && rm -rf ~/herdeck/src/herdeck && mv
~/herdeck.old ~/herdeck/src/herdeck`, then kickstart again. The `test -d` is
load-bearing: without it a rollback with no backup deletes the live tree. Never
delete the whole checkout — an editable venv inside it is what the unit runs.
**`git archive` does not carry `node_modules`**: run `npm ci` in `desktop/`
before building the app whenever frontend dependencies changed.

## Restarting services

`herdeck-service restart <kind> [--system]` runs the matching command below.

```bash
launchctl kickstart -k gui/$(id -u)/dev.herdeck.runtime   # runtime
launchctl kickstart -k user/$(id -u)/dev.herdeck.bridge   # bridge LaunchAgent
sudo launchctl kickstart -k system/dev.herdeck.bridge     # bridge --system
systemctl --user restart herdeck-<kind>.service           # Linux
```

Some long-running services drift out of launchd's supervision — a plist gets
renamed while the process keeps running, so it survives with `ppid 1` and
nothing restarts it if you kill it. Check before restarting:

```bash
launchctl list | awk -v pid=PID '$1 == pid'   # blank means nothing supervises it
```

If it is unsupervised, move it under `herdeck-service` (see the migrations
above) rather than restarting it by hand.

**Do not recover a token by parsing `ps eww`.** It truncates, and a silently
truncated token produces a service that starts, listens, and never connects.
The generated units carry `HERDECK_TOKEN_FILE` — the path, never the value —
which is what keeps the token out of `ps eww` in the first place. The legacy
`HERDECK_TOKEN` env var is still accepted, but passing the value that way is the
reason it shows up in process listings at all.

If the token is lost or leaked, replace it instead of recovering it:
`herdeck-bridge --rotate-token` writes a new one to the same file and lists the
restart and runtime-side steps. Until the bridge restarts it keeps accepting the
old token only.

A bridge that suddenly will not start after an update may be refusing its
address: since 0.8.1 `herdeck-bridge` enforces the same bind policy as
`herdeck-web` (loopback or Tailscale). The log says `refusing to start:
HERDECK_BIND must be loopback or a Tailscale address`; fix `HERDECK_BIND`, or set
`HERDECK_ALLOW_UNSAFE_BIND=1` if the exposure is deliberate.

## Reviving the D200 without a terminal

The runtime answers three token-authenticated maintenance routes (the desktop
Maintenance section is built on them; the token is the one in
`~/.cache/herdeck/runtime.json`):

- `GET /maintenance?token=...` — version, pid, uptime, whether the runtime
  service is installed and runs from the app, log paths, D200 state (`state`:
  `connected`, `not_on_usb`, `locked`, `disconnected`, `unsupervised`), whether
  the D200 is on USB, its last-seen hub location and whether a power-cycle is
  possible, and per-bridge health. Each server also carries `self_update`
  (the bridge understands `update`) and `managed` (`true`/`false` from one
  health probe per connection; `null` until answered, on timeout, or for a
  bridge that predates self-update).
- `POST /maintenance/deck/restart` (`X-Herdeck-Token`) — closes and reopens the
  D200 and repaints a full frame. It never releases `d200.lock`; when another
  runtime owns the device it answers `locked_by` with that pid instead.
- `POST /maintenance/deck/power-cycle` — runs `uhubctl -l <hub> -p <port> -a
  cycle -d 2` (no shell, 30 s timeout). It needs `uhubctl` (`brew install
  uhubctl`; found on PATH, in the Homebrew prefixes, or at `[hardware].uhubctl`)
  and the D200's port: the runtime remembers where it last saw the device in
  `~/.cache/herdeck/d200-usb.json`, or pin it with `[hardware].usb_hub` /
  `[hardware].usb_port`. Only hubs with per-port power switching can cut power.
  When uhubctl needs root it answers `needs_admin` with the exact `sudo`
  command to run.

`POST /maintenance/servers/<id>/update` is reserved for the bridge self-update
and answers `501` until it ships.

## Rebuilding the desktop app

```bash
(cd desktop && npm ci)        # only if dependencies changed
bash desktop/scripts/build-app.sh
```

The build's **only expected failure** is the updater signature:

```
A public key has been found, but no private key.
Make sure to set `TAURI_SIGNING_PRIVATE_KEY` environment variable.
```

That is printed *after* the bundle is written. The `.app` under
`desktop/src-tauri/target/release/bundle/macos/` is complete; install it by
copying over the existing one.

### The one check that says the install worked

After launching, count processes. The correct state is exactly **two**: the
runtime, and the desktop binary. A third process under
`herdeck.app/Contents/Resources/herdeck-deckapp/` means the window did not
find a healthy runtime at launch and spawned its own frozen sidecar. That
happens when the runtime is not up at the moment the app launches, e.g. during
an auto-update relaunch. It is no longer permanent:

- The app re-reads `runtime.json` every 12 s (and on any failed poll). Once the
  launchd runtime's `/health` answers, the window switches to it and stops its
  own sidecar, so the third process should disappear within about 15 s.
- The sidecar never opens the D200 while the runtime holds
  `~/.cache/herdeck/d200.lock`. If the sidecar got the lock first, the runtime
  takes the deck over within 5 s of the sidecar exiting.
- A spawned sidecar exits when the app dies, including a crash or Force Quit:
  its stdin is a pipe from the app, and it shuts down cleanly on EOF.

The app log (`~/Library/Logs/herdeck/herdeck.log`) shows each decision:

```
herdeck: runtime plan=spawn reason=runtime_unhealthy
herdeck: runtime plan=attach reason=launchd_runtime_appeared url=http://127.0.0.1:52001
herdeck: stopping own sidecar pid=4242
```

Other reasons: `runtime_json_healthy` (normal attach at launch),
`no_runtime_json`, `own_sidecar_unreachable` (switched after a failed poll),
`runtime_restarted` (the launchd runtime came back on a new port),
`env_override`, and `attach_disabled_for_channel` (dev builds never attach).
`plan=spawn reason=attached_runtime_lost` means the app had switched to the
launchd runtime, which then stayed unreachable (3 failed re-discoveries over at
least 30 s), so the app started its own sidecar again. It switches back once
the runtime is healthy.
A third process that stays for longer than about 15 s means the launchd runtime
is not answering `/health`; check the runtime itself.

### A banner arrived through `osascript`

With the app installed, the app posts every banner natively. The runtime falls
back to `osascript` in only two cases, and it logs a WARNING with the reason
for each:

- `notification fallback=osascript reason=no_shell_claim last_claim_age=72s`:
  no app had polled `/notifications` in the last 60 s (`never` = no app since
  the runtime started). Look for the matching `notification shell claim
  lapsed` and `… acquired … (after N s without one)` lines to see how long the
  gap was, and for `runtime plan=` lines around the same time.
- `notification fallback=osascript reason=shell_native_failed id=… error=…`:
  the app tried to post the banner and macOS refused it (the error is the
  app's). Check the notification permission in System Settings.

## Verifying without a display

`~/.cache/herdeck/runtime.json` carries the runtime's URL and token. The GET
probes below take it as a **query parameter**, not a bearer header. The POST
routes are different: `/press/N` wants it in an `X-Herdeck-Token` header.

Three layers sit between a bridge push and a lit key, and each check below sees
exactly one of them. Reading a check from the wrong layer is how a stalled deck
gets declared healthy:

| layer | what it is | what shows it |
|---|---|---|
| source | `LiveSource._agents`, fed by bridge pushes | `/state`'s `summary`, `/health`'s `connected` |
| render | tiles and panel rasterised into the HTTP buffer | `/state`'s `version`, `/panel`, `/tile/N` |
| delivery | the frame handed to each sink and written to the device | `/health`'s `d200` (`last_frame_at`, `last_error`, `lock_owner`), the log, and your eyes |

**Source.** `/health` reports the runtime's own view of its bridge links, and
`summary` is counted from the agent records at request time — neither touches
the render pipeline:

```bash
curl "$URL/health?token=$TOKEN"   # {"ok": true, "connected": true, ...}
curl "$URL/state?token=$TOKEN"    # slots, panel, tile versions, agent summary
```

`/health` also carries `version` (proof the new code is what is running),
`uptime_s` (proof it restarted), and per server under `servers` the
`last_error`, `attempt` count and the `bridge_version` the bridge announced.
`herdeck-doctor` on the render host reads the same data and additionally asks
each bridge for its own health (`herdr_reachable`, attached `clients`) — the
`clients` count is the bridge-side view described next, without `lsof`.

From the bridge host, `lsof` is the other end of the same layer:

```bash
lsof -a -p BRIDGE_PID -i -Pn | grep ESTABLISHED
```

Neither view alone separates "nobody ever attached" from "the link died": both
show zero established, and a half-open link can leave the bridge holding a
socket the runtime has already given up on. It is the *disagreement* that tells
you — agreement on zero means nothing attached; `/health` claiming connected
while the bridge shows no socket, or the reverse, means one end has not noticed
the link is gone.

Healthy is one established connection per attached client: each render host,
plus any web cockpit. A count above that means something extra attached — a
stale sidecar, for instance, which is what the process count above is for.

**Render.** A rising `version` means the runtime is rasterising. It bumps once
per changed tile plus the panel, and only tiles on screen are versioned — the
deck pages agents through its slots, so agents paged off contribute nothing and
the rate tracks visible tiles, not agent count. A full 13-slot deck with most
agents inside the 5-second bucket measured ~20 in 12 seconds; watch whether it
moves, not how fast.

The images are the same layer, and they are stored bytes rather than fresh
renders — the endpoints never re-render on the request path, so a runtime whose
ticker died after its first frame serves the same PNG forever. Compare two
fetches spaced wider than the bucket the tiles are currently in:

```bash
curl -sf "$URL/panel?token=$TOKEN" -o /tmp/panel.png && file /tmp/panel.png
curl -sf "$URL/tile/0?token=$TOKEN" -o /tmp/t.png && shasum /tmp/t.png
```

Fetch to a file and gate on `&&`. Piping into `shasum` hides the failure it is
most likely to hit: a rejected token returns a constant error page, so two runs
hash the same and it reads as "not repainting". `--fail` alone does not save
you, because a pipeline reports `shasum`'s exit status, not `curl`'s.

**Before calling a frozen hash or version a stall, check what the deck is
showing.** Both are computed from rendered bytes, so anything static on screen
holds them still legitimately. The elapsed bucket catches people out: it steps
every 5 seconds under a minute, every minute under an hour, every hour beyond —
so on a long-running deployment, exactly the deck you look at after a deploy, an
agent showing `12m` changes its tile once a minute. A deck left in a drill or a
menu view carries no elapsed text at all. And every slot is rasterised whether
or not an agent occupies it, so on a deck whose agents have gone away `/tile/0`
is a blank tile that hashes the same forever — it is only a meaningful probe
while `summary`'s `agents` count is non-zero.

To force the question, put the deck back in the overview and then cause an
agent status change yourself.

Both halves are doable blind while `summary` reports at least one agent. In that
case, any `view` value in `/state`'s `tile_sections` proves that an agent tile is
on screen and the deck is in the overview. The other section names do not prove
the opposite: the overview's launcher and management tiles also report
`start_profiles` or `profiles`.

Only when agents exist and `tile_sections` contains no `view`, press the last
slot. It is the Back tile in every menu and drill, but in the overview that same
slot opens the launcher or another management action:

```bash
curl -X POST -H "X-Herdeck-Token: $TOKEN" "$URL/press/$((SLOTS-1))"
```

`SLOTS` is `/state`'s `slots`. Re-read `tile_sections` after the `204`; the
runtime has already refreshed that state before replying. Back from a profile
menu can return to the launcher when the menu was opened there, so repeat the
conditional check and press until a `view` entry appears. If the sections do
not change after a press, the press did not do what you assumed — waiting will
not make that state transition arrive later.

With no agents, the overview has no `view` tile and cannot be distinguished
from the launcher by section names alone. More importantly, there is no agent
whose status can be changed, so this forced-change verification does not apply.

**Do it from the overview**: the launcher and the profile menu render no agent
tiles and not the overview panel, so a status change there moves nothing and a
healthy deck looks stalled.

A drill view is narrower rather than dead: the drilled agent's own status moves
its panel and its action tiles, so forcing the change on *that* agent works.
Forcing it on any other leaves the frame identical.

From the overview, split what you expect. The panel carries the status counts,
so `version` moves even when the agent that changed is paged off screen. The
tile hash may not: the deck pages, so `/tile/0` can stay byte-identical while
the deck works perfectly — a frozen tile is not evidence of a stall. It is not a
biconditional either, since a status change that alters an agent's rank can
re-sort the display and pull a paged-off agent into view.

`summary` proves only that the change reached the source. `summary` moving while
`version` stays flat, with the deck in the overview, is the stall signature.

**Delivery is not covered by any of this.** The version is bumped when the frame
lands in the HTTP buffer, before it is handed to the sinks, and a sink that
raises is isolated and only logged. A dark device with a rising version is a
delivery problem — this repo has hit exactly that, with `/panel` and `/tile/N`
correct while the device showed black. Look for `render sink ... failed to
deliver a frame` in the runtime's log: `~/Library/Logs/herdeck-runtime.log` for
a `herdeck-service` unit, `journalctl --user -u herdeck-runtime` on Linux. A
hand-made plist without `StandardErrorPath` discards the one piece of evidence
this layer has — one more reason to migrate it (see above).

### Checking what the built UI says

Svelte compiles a dynamic `{expression}` into a runtime text node, so the
template markup in the bundle shows an **empty** element — grepping the markup
proves nothing. Grep the built asset for the value instead:

```bash
grep -oE ".{40}$(cat VERSION | sed 's/\./\\./g').{20}" desktop/build/assets/index-*.js
# -> Ct.textContent=`v0.2.0`
```

Inside the installed `.app` even that fails: Tauri compresses `frontendDist`
into the binary, so `strings` finds neither the old value nor the new one, and
that absence is not evidence. Check `desktop/build/assets/` before the bundle
step, plus:

```bash
/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" \
  /path/to/herdeck.app/Contents/Info.plist
```
