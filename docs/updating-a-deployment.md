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
**query parameter**, not a bearer header.

Three layers sit between a bridge push and a lit key, and each check below sees
exactly one of them. Reading a check from the wrong layer is how a stalled deck
gets declared healthy:

| layer | what it is | what shows it |
|---|---|---|
| source | `LiveSource._agents`, fed by bridge pushes | `/state`'s `summary`, `/health`'s `connected` |
| render | tiles and panel rasterised into the HTTP buffer | `/state`'s `version`, `/panel`, `/tile/N` |
| delivery | the frame handed to each sink and written to the device | the log, and your eyes |

**Source.** `/health` reports the runtime's own view of its bridge links, and
`summary` is counted from the agent records at request time — neither touches
the render pipeline:

```bash
curl "$URL/health?token=$TOKEN"   # {"ok": true, "connected": true, ...}
curl "$URL/state?token=$TOKEN"    # slots, panel, tile versions, agent summary
```

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
agent status change yourself. **Do it from the overview**: the launcher and the
profile menu render no agent tiles and not the overview panel, and a drill view
renders only the drilled agent's action tiles, so in any of them a status change
moves nothing and a healthy deck looks stalled.

From the overview, split what you expect. The panel carries the status counts,
so `version` moves even when the agent that changed is paged off screen. The
tile hash only moves if that agent is on the visible page — the deck pages, so
`/tile/0` can stay byte-identical while the deck is working perfectly.

`summary` proves only that the change reached the source. `summary` moving while
`version` stays flat, with the deck in the overview, is the stall signature.

**Delivery is not covered by any of this.** The version is bumped when the frame
lands in the HTTP buffer, before it is handed to the sinks, and a sink that
raises is isolated and only logged. A dark device with a rising version is a
delivery problem — this repo has hit exactly that, with `/panel` and `/tile/N`
correct while the device showed black. Look for `render sink ... failed to
deliver a frame` in the runtime's log — the path is the `StandardErrorPath` of
the launchd job you restarted above.

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
