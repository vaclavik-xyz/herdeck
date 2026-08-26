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
to rendering (`layout.py`, `orchestrator.py`, `icons.py`, `protocol.py`) only
take effect on the render host. Both hosts usually need the update, and the wire
format is designed so a new bridge and an old runtime keep working — additive
snapshot fields are ignored by older clients, so the two do not have to move in
lockstep.

The desktop app is the exception: its frontend is compiled into the bundle, so
it does **not** pick up changes from a source sync. It needs a rebuild.

## Syncing source

Where the deploy target is a checkout rather than an installed package, a
`git archive` stream is the simplest way in:

```bash
git archive --format=tar main | ssh HOST 'tar -x -C ~/path/to/herdeck'
```

Back the tree up first if the target has ever been hand-edited.

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
`HERDECK_TOKEN` is the content of the bridge token file (see the README); read
the file.

## Rebuilding the desktop app

```bash
cd desktop && npm ci          # only if dependencies changed
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

A `version` that rises between two `/state` calls means the deck is painting,
not merely connected. On the bridge host, one established connection per client
should be visible:

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
