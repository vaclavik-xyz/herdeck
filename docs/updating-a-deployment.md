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

Changing `protocol.py`'s encode/decode contract has no safe order at all; both
hosts move together.

The desktop app is the exception: its frontend is compiled into the bundle, so
it does **not** pick up changes from a source sync. It needs a rebuild.

## Syncing source

Where the deploy target is a checkout rather than an installed package, a
`git archive` stream is the simplest way in:

```bash
git archive --format=tar main | ssh HOST 'tar -x -C ~/path/to/herdeck'
```

Back the tree up first if the target has ever been hand-edited.

`tar -x` only adds and overwrites — it never deletes. A module removed in the
release stays on the target and stays importable, so the host can keep running
deleted code with nothing to show for it. When a release removes files, clear
the synced subtree first:

```bash
ssh HOST 'rm -rf ~/herdeck-src.old && mv ~/path/to/herdeck/src/herdeck ~/herdeck-src.old'
git archive --format=tar main | ssh HOST 'tar -x -C ~/path/to/herdeck'
```

Move it aside rather than deleting it, and stop the service for that window.
The runtime imports lazily well after startup, so an import landing in the gap
raises `ImportError` on a live process — and if the stream or the connection
dies mid-transfer, the moved-aside copy is the rollback:

```bash
ssh HOST 'test -d ~/herdeck-src.old \
  && rm -rf ~/path/to/herdeck/src/herdeck \
  && mv ~/herdeck-src.old ~/path/to/herdeck/src/herdeck'
```

The `test -d` is load-bearing. Without it, a restore run where the backup is
missing — the move-aside was skipped, or the restore already succeeded once —
deletes the current tree and then fails, leaving no `src/herdeck` at all. That
is worse than the deploy it was rolling back, reached by running the documented
rollback under exactly the stress it is written for.

Clear the destination in both directions. `mv` into a directory that already
exists nests inside it instead of replacing it, so a second deploy would leave
the first deploy's tree at the documented rollback path and the real backup one
level down.

**Do not delete the whole checkout.** The venv usually lives inside it and is
installed editable, so the launchd unit's `ProgramArguments` points straight at
`.venv/bin/python` — removing the tree breaks the service on what was supposed
to be a routine sync. It would take `node_modules` with it too.

`rsync` in one step is possible but is not the same thing: it ships your working
tree, uncommitted edits included, rather than the committed state of `main`, and
`--delete` will remove build artifacts the rest of this document reads. If you
use it, exclude all of them:

```bash
rsync -a --delete --exclude .git --exclude .venv --exclude node_modules \
  --exclude target --exclude desktop/build ./ HOST:~/path/to/herdeck/
```

**`git archive` does not carry `node_modules`.** If the release added a frontend
dependency, the synced `package.json` will reference a package that is not
installed, and the Tauri build dies with `Rolldown failed to resolve import`.
Run `npm ci` in `desktop/` before building whenever dependencies changed.

## Restarting services

Under launchd:

```bash
launchctl kickstart -k gui/$(id -u)/LABEL
```

Some long-running services drift out of launchd's supervision — a plist gets
renamed while the process keeps running, so it survives with `ppid 1` and
nothing restarts it if you kill it. Check before restarting:

```bash
launchctl list | awk -v pid=PID '$1 == pid'   # blank means nothing supervises it
```

If it is unsupervised, restart it by hand with the same environment rather than
re-enabling the plist — it was disabled deliberately.

**Do not recover a token by parsing `ps eww`.** It truncates, and a silently
truncated token produces a service that starts, listens, and never connects.

Restart with `HERDECK_TOKEN_FILE` pointing at the token file (mode `0600`), the
way the generated launchd units do — they carry the path and never the value,
which is what keeps the token out of `ps eww` in the first place. The legacy
`HERDECK_TOKEN` env var is still accepted, but passing the value that way is the
reason it shows up in process listings at all.

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
`herdeck.app/Contents/Resources/herdeck-deckapp/` means the window failed to
attach to the running runtime and spawned its own frozen sidecar — which then
competes with the runtime for the deck. That happens when the runtime is not up
at the moment the app launches, and it persists silently until the app is
restarted.

## Verifying without a display

`~/.cache/herdeck/runtime.json` carries the runtime's URL and token. Auth is a
**query parameter**, not a bearer header:

```bash
curl "$URL/health?token=$TOKEN"   # {"ok": true, "connected": true, ...}
curl "$URL/state?token=$TOKEN"    # slots, panel, tile versions, agent summary
```

A rising `version` between two `/state` calls means the runtime is rendering.
On a default deck it rises on its own, because `time` is a default tile field
and the periodic full refresh re-renders so the elapsed text advances. It bumps
once per changed tile plus the panel, so the rate tracks how many tiles are
moving, not a fixed cadence — a 28-agent deck measured ~20 in 12 seconds, while
a healthy two-agent deck may only manage 2-4. Watch whether it moves, not how
fast.

**It does not mean the physical deck received anything.** The version is bumped
when the frame lands in the HTTP buffer, before it is handed to the sinks, and a
sink that raises is isolated and only logged. A dark device with a rising
version is a delivery problem, not a render stall — this repo has hit exactly
that, with `/panel` and `/tile/N` correct while the device showed black. Look
for `render sink ... failed to deliver a frame` in the log.

The images tell you a frame exists, not that one is being made: the endpoints
serve the bytes of the last successful refresh and never re-render on the
request path, so a runtime whose ticker died after its first frame serves the
same PNG forever. Compare two fetches instead of looking at one — spaced wider
than the bucket the tiles are currently in:

```bash
curl -sf "$URL/panel?token=$TOKEN" -o /tmp/panel.png && file /tmp/panel.png
curl -sf "$URL/tile/0?token=$TOKEN" -o /tmp/t.png && shasum /tmp/t.png
```

Fetch to a file and gate on `&&`. Piping into `shasum` hides the failure it is
most likely to hit: a rejected token returns a constant error page, so two runs
hash the same and it reads as "not repainting" — a false stall on top of the
check meant to find real ones. `--fail` alone does not save you, because a
pipeline reports `shasum`'s exit status, not `curl`'s.

**Before reading a frozen hash or a frozen version as a stall, check what the
deck is showing.** Both are computed from rendered bytes, so anything static on
screen holds them still legitimately.

The one that catches people out is the elapsed bucket. It steps every 5 seconds
under a minute, every minute under an hour, and every hour beyond — so on a
long-running deployment, which is exactly the deck you look at after a deploy,
an agent showing `12m` changes its tile once a minute and one showing `3h` once
an hour. Two fetches seconds apart hash identically on a perfectly healthy deck.
Space them wider than the bucket you can see on the tiles.

A deck left in a drill or a menu view carries no elapsed text on any tile at
all, and a deck with no agents renders the same blank slots every time. Put it
back in the overview first, or skip the waiting entirely: cause an agent status
change yourself and watch `/state`'s `summary` move.

From the bridge host, confirm the far end actually attached — `/health`'s
`connections` is the runtime's own view, and this is what tells "the bridge is
up but nobody is connected" apart from "the runtime lost the link":

```bash
lsof -a -p BRIDGE_PID -i -Pn | grep ESTABLISHED
```

Expect one established connection per client: each render host, plus any web
cockpit.

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
