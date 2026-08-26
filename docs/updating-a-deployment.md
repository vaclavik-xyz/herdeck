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

Adding a field to the snapshot is safe to roll out one host at a time — older
clients ignore fields they do not know. Changing `protocol.py`'s encode/decode
contract is not, and that exemption does not cover it.

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
deleted code with nothing to show for it. When a release removes files, delete
the target tree and extract into an empty one, or remove them by hand.

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

A `version` that rises between two `/state` calls means the deck is painting,
not merely connected. It only bumps when tile or panel bytes actually change,
though, so a quiet deck holds a static version indefinitely — that means
"nothing to repaint", not "not painting". To be sure, either watch it across an
agent status change, or fetch the images directly:

```bash
curl -s "$URL/panel?token=$TOKEN" | file -
curl -s "$URL/tile/0?token=$TOKEN" | file -
```

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
