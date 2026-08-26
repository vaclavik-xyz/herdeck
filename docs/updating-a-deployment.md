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
ssh HOST 'rm -rf ~/path/to/herdeck/src/herdeck && mv ~/herdeck-src.old ~/path/to/herdeck/src/herdeck'
```

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

A `version` that rises between two `/state` calls means the runtime is
*rendering* — and on a default deck it should rise on its own. `time`
is a default tile field, elapsed text steps every 5 seconds under a minute and
every minute under an hour, and the periodic full refresh re-renders exactly so
that advance reaches the deck. **So a version that has not moved for several
minutes on a deck with agents is a stall signature, not a quiet one.**

It genuinely holds static only with no agent tiles, with `time` removed from
`[view].tile_fields`, or once every agent has sat in one status past the hour
bucket.

**A rising version does not mean the physical deck received anything.** The
version is bumped when the frame lands in the HTTP buffer, before it is handed
to the sinks, and a sink that raises is isolated and only logged. So a dark
device with a rising version is a delivery problem, not a render stall — this
repo has hit exactly that, with `/panel` and `/tile/N` correct while the device
showed black. Look for `render sink ... failed to deliver a frame` in the log.

Fetching the images tells you a frame exists, not that one is being made — the
endpoints serve the bytes stored by the last successful refresh and never
re-render on the request path, so a runtime whose ticker died after its first
frame serves the same PNG forever:

```bash
curl -sf "$URL/panel?token=$TOKEN" -o /tmp/panel.png && file /tmp/panel.png
curl -sf "$URL/tile/0?token=$TOKEN" -o /tmp/t.png && shasum /tmp/t.png
```

Fetch to a file and gate the check on `&&`. Piping into `shasum` hides the
failure it is most likely to hit: a 404 (no agents, or slot 0 empty) returns an
empty body and a rejected token returns a constant error page, so either way two
runs hash the same and it reads as "not repainting" — a false stall on top of
the check meant to find real ones. Do not treat any particular hash as the tell.
`--fail` alone does not save you, because a pipeline reports `shasum`'s exit
status, not `curl`'s; with `-sf` a failure prints nothing at all, so add
`-w '%{http_code}'` when you need to tell a 403 from a 404.

Two hashes about fifteen seconds apart are enough on a default deck: tile bytes
carry the elapsed text, and the periodic full refresh comes round roughly every
ten seconds. The same caveats as above apply — no agents, no `time` field, or
everything past the hour bucket, and the bytes legitimately stop changing.

One more is specific to this check: slot 0 only holds an agent tile in the
overview. In a drill or a menu view it is a static option or label tile with no
elapsed text, so a deck someone was poking at during the deploy gives two
identical hashes while rendering perfectly. Fall back to `/state`'s `version`,
which the panel drives too.

On the bridge host, one established connection per client should be visible:

```bash
lsof -a -p BRIDGE_PID -i -Pn | grep ESTABLISHED
```

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
