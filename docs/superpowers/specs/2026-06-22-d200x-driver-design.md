# Ulanzi D200x integration — direct-HID herdeck-native driver (phased)

**Date:** 2026-06-22
**Status:** Approved design (phased v1/v2). Written spec pending review before writing-plans.
**Scope:** Add herdeck support for the new **Ulanzi D200x** (14 LCD keys + 3 rotary knobs + 2 side buttons). The hardware is **not yet owned** (evaluating before purchase), so this milestone delivers a **paper spec + an HW-free implementable plan** — everything except a standalone probe and a final reconcile is writable, unit-testable, and mergeable **without the device**.

## Goal

herdeck drives the D200x as a deck front-end, reusing the **grid Orchestrator** exactly like the D200, via a **herdeck-native direct-HID protocol module** (no `strmdck` fork). Knobs add agent navigation in v1 and, in v2, on-deck prompt answering. The implementation is verified against **documented community reverse-engineering**, with a standalone **probe** that confirms/corrects the constants once a unit is purchased.

## Context (what we know)

- **D200x hardware:** 14 customizable LCD keys, 3 rotary knobs (rotate CW/CCW + press), 2 side buttons, 5.5″ 960×540 screen, USB 3.2, 8-in-1 dock. Launched 04/2026. It is **not a Stream Deck** — it runs Ulanzi's own Studio app and is explicitly **incompatible with Elgato Stream Deck plugins**, so herdeck's freshly-merged Elgato path does **not** apply; the HID-deck path (like the D200) does.
- **Protocol (community reverse-engineering):** `redphx/strmdck` mapped the D200; `Tyaaa-aa/Ulanzi-Deck-Linux` claims D200/**D200x**. Per that work the D200x shares the D200's **USB VID/PID `0x2207:0x0019`**, the same **`0x7c7c` framing** (1024-byte packets, `cmd:u16` big-endian, `len:u32`), the same **render path** (a ZIP of `manifest.json` keyed `col_row` + per-key **196×196 PNG** under `icons/`, with a dummy-padding workaround for invalid bytes at 1024-byte boundaries), and the same brightness/button commands. The D200x **adds rotary input** reported as indices **17/18/19** via a second input command **`IN_BUTTON_2 = 0x0102`** carrying a `dial_event` byte (rotation direction / press). **Every such value is an assumption from community RE and must be confirmed by the probe.**
- **herdeck today:** drives the D200 via `strmdck` (`src/herdeck/driver/d200.py`) implementing the `DeckDriver` interface and plugging into the **grid Orchestrator** (`src/herdeck/orchestrator.py`). The Orchestrator focuses a single agent via a **drill** concept (`self._drill`), entered by **pressing that agent's tile** (`self._drill = key`, which focuses the herdr pane + reads the prompt and shows drill action tiles). There is **no roaming selection cursor** — selection happens by direct tile press.

## Key decisions (locked in brainstorming)

- **Phased.** **v1 = parity** (14-key grid via the existing Orchestrator) **+ light knobs** (agent navigation, brightness, Approve/Deny). **v2 = value-add** (on-deck multi-option answer, refined full-selection scrub). The spec describes both; the plan delivers v1 with v2 marked as a separate later phase.
- **Direct-HID, herdeck-native.** A new herdeck protocol module owns the `0x7c7c` protocol. **No `strmdck` fork.** The D200 stays on `strmdck`, unchanged (no churn to working code). The protocol module is **shared by the probe and the driver**.
- **HW-free implementable.** Everything except the probe and a final reconcile task is unit-testable against documented constants. The constants live in **one clearly-marked block**, each value tagged *"assumed from community RE — confirm via probe."* The plan is mergeable without the hardware.
- **Explicit deck kind for v1.** `kind = "d200x"` is selected **explicitly** (config/env), **not** auto-detected. D200 and D200x share the VID/PID, so disambiguation needs the `IN_DEVICE_INFO` (`0x0303`) discriminator bytes, which are **unknown until the probe runs**. `make_deck` already supports an explicit `kind` (`kind=None` ⇒ auto), so `"d200x"` is added as an explicit option. Auto-detect is a **v2 item gated on the probe**. This is also a safety guard: a wrong auto-guess could mistake a D200 for a D200x.
- **Probe is Phase 0, separate from the plan's HW-free tasks.** A standalone read-only tool — writable now, run on delivery day — confirms/corrects the constants; a final reconcile task closes the HW tail.

## Out of scope

- **Auto-detecting D200 vs D200x** by USB/device-info (v2, gated on the probe discriminator).
- **v2 value-add knobs** (on-deck multi-option answer, refined selection scrub) — designed here, implemented later.
- **Migrating the D200 off `strmdck`** (no churn to working code; the two transports coexisting is accepted).
- **The dock's non-deck functions** (HDMI / card readers / USB hub) — those are OS-level, nothing for herdeck.
- **Windows**, code signing, real (non-placeholder) icon art (shared with the broader herdeck follow-ups).

## Architecture

### Front-end: reuse the grid Orchestrator

The D200x is a 14-key deck, so `D200xDriver` implements the existing **`DeckDriver`** interface (`render(tiles)`, `render_panel`, `render_working`, `on_press`, `on_dial`, `close`, `run_reader`, `keep_alive_loop`) and plugs into the grid Orchestrator like the D200. All existing herdeck logic — TileView rendering, status colors, the status panel, press → drill/action — is reused unchanged. The **only new surface** is the knob / side-button event channel.

### Transport: herdeck-native protocol module

`src/herdeck/driver/ulanzi_proto.py` (**NEW**) — pure-logic D200x protocol, no hardware:

- `encode_command(cmd: int, payload: bytes) -> bytes` — `0x7c7c` framing (header + `cmd` u16 BE + `len` u32 + data), chunked into 1024-byte packets.
- `decode_input(data: bytes) -> KeyEvent | DialEvent | None` — parse `IN_BUTTON` (`0x0101`) and `IN_BUTTON_2` (`0x0102`): a key index + pressed flag for buttons; a knob index (17/18/19) + direction/press for dials. Returns `None` for non-input packets.
- `build_render_zip(buttons: dict[int, RenderSpec]) -> bytes` — `manifest.json` keyed `f"{col}_{row}"` (5-column grid) + per-key 196×196 PNG under `icons/`, including the dummy-padding workaround that avoids invalid bytes (`0x00`, `0x7c`) landing on 1024-byte packet boundaries.
- **Constants block** — VID/PID, command codes, dial indices, key count, packet/chunk sizes — in one clearly-marked section, **each value tagged "assumed from community RE — confirm via probe."**

Fully unit-testable without hardware: framing round-trips, dial parsing from documented byte layouts, render ZIP/manifest structure, and the byte-boundary workaround.

### Driver: D200xDriver

`src/herdeck/driver/d200x.py` (**NEW**) — `D200xDriver(DeckDriver)`:

- Opens the HID device via `hid` (hidapi), reusing the D200's **control-interface-by-path** approach (HID `usage_page 0x0c`, preferring the control interface, retry-on-open).
- `render` / `render_panel` / `render_working` build payloads via `ulanzi_proto.build_render_zip` from the existing 196×196 TileView icons (`IconProvider.render_tile`) and the panel composition.
- `run_reader` reads HID packets, runs each through `ulanzi_proto.decode_input`, and dispatches `KeyEvent` → the `on_press` callback (debounced as in d200) and `DialEvent` → the **`on_dial`** callback.
- brightness + keep-alive via `encode_command`.
- **Shared panel helpers:** `split_panel` currently lives in `d200.py`. If the D200x reuses it cleanly, lift it to a shared location (e.g. `icons.py` alongside `compose_panel`); otherwise accept a small duplication. **Do not refactor the working D200 driver solely for this.**

### Knob / side-button event channel

- The base **`DeckDriver`** gains **`on_dial(callback)` as a concrete default no-op** (not abstract), so the d200 / web / fake / elgato drivers are untouched and need no changes.
- A `DialEvent` carries: the knob index, a rotation delta (or direction) and a press flag — enough for the Orchestrator to act without knowing wire details.
- **Orchestrator dial routing (v1):** reuses the Orchestrator's existing **drill/focus** machinery — a knob **press** drills into the currently-selected agent exactly like a tile press (`self._drill = key`) — plus brightness (driver-local). The one small new Orchestrator-side concept v1 adds is a **roaming selection cursor** over agents (the grid model lacks it — today you select by pressing a specific tile). The exact integration is finalized when writing-plans reads `orchestrator.py`.
- **v2 hook:** the Orchestrator exposes the selected blocked agent's **multi-option prompt options + a highlighted index** to the dial handler, enabling on-deck answering (see v2 below). Marked as a separate phase.

### Probe (Phase 0, standalone — its own deliverable)

`tools/d200x_probe.py` (**NEW**) — read-only, written now, run once on the real device:

1. enumerate HID; confirm VID/PID; list interfaces + `usage_page`s.
2. query `IN_DEVICE_INFO` (`0x0303`) and dump the response — the **D200-vs-D200x discriminator** (feeds the v2 auto-detect).
3. dump raw input packets while the user presses keys / rotates + presses each knob / presses each side button — reveals the **real key/dial/button index + byte encodings**.
4. one **test render** (a single key PNG) to confirm the render path + on-screen layout.

Its output **reconciles the documented-constants block**.

## Knob & button design

### v1 (light, additive) — PROVISIONAL mapping

> The exact mapping and the knob/button **count** are **provisional**, confirmed by the probe. The mapping lives in the driver and is cheap to rearrange — it **must not block the spec**, and exact wire indices are not bikeshedded now. Filip may revise the concrete assignment; it is low-cost.

Principle: a knob's value is a **continuous selection across agents** (there are more agents than keys), plus quick attention navigation.

- **Knob 1 — global selection scrub:** rotation moves a roaming selection cursor across **all** agents; **press focuses** (drills into) the selected agent's pane. This is a superset of a pager — it reaches attention-needing agents too, and the panel shows the selected agent's state.
- **Knob 2 — brightness** (optionally profile switching).
- **Knob 3 — pager:** advance the selection cursor to the **next attention-needing (blocked) agent**; press = focus.
- **2 side buttons — global Approve / Deny** of the selected blocked agent (binary prompts only, the same gating as the elgato front-end: blocked + online + prompt read + yes/no with no numbered options).

### v2 (value-add, separate phase)

- **On-deck multi-option answer:** when the selected blocked agent has a multi-option prompt, a knob rotates among the options (highlighted on the panel / a tile) and a press sends the choice. This removes today's limitation — multi-option prompts are currently answerable only by focusing the TUI. Requires the Orchestrator to expose the prompt's option list + highlighted index to the dial handler.
- **Refined full-selection scrub** semantics across all agents/servers (filters, ordering).

## Testing

- **HW-free (the whole plan except probe + reconcile):**
  - `ulanzi_proto` unit tests: framing encode/decode round-trip; `decode_input` key + dial parsing from documented byte layouts; `build_render_zip` manifest/ZIP structure + the invalid-byte boundary workaround.
  - `D200xDriver` logic against a **fake HID device**: render calls produce the expected payloads; injected input bytes dispatch the right `on_press` / `on_dial` events; brightness/keep-alive encode correctly.
  - All assertions are against the **documented-constants block**.
- **Probe (run on delivery day):** confirms VID/PID, the device-info discriminator, the real input encodings, and the render path.
- **Reconcile + HW manual gate (final task, post-purchase):** update any constant the probe corrected, then exercise `D200xDriver` on the real device — render 14 keys, key presses, all 3 knobs (rotate + press), 2 side buttons, and the panel.

## Risks / probe-confirmed unknowns

1. **Shared VID/PID** D200↔D200x → explicit config for v1; auto-detect via device-info is v2 (the probe supplies the discriminator).
2. **Exact dial indices/codes** (17/18/19 + `dial_event`) from community RE — probe confirms.
3. **14-key layout + panel/clock cell positions** (the D200 uses indices 13/14 for its 2-cell panel; the D200x may differ) — probe confirms.
4. **Render assumption** (firmware composites per-key 196×196 PNGs onto the 960×540 screen) — probe confirms.
5. **Knob/button count + mapping** — provisional, driver-local; the probe + Filip's preference finalize it.

## Plan shape (input for writing-plans)

- **Phase 0 (standalone, no HW dependency to *write*):** `tools/d200x_probe.py`.
- **Plan (HW-free, mergeable now), roughly:** (1) `ulanzi_proto` constants block + framing; (2) `decode_input` key/dial parsing; (3) `build_render_zip`; (4) `DeckDriver.on_dial` concrete no-op on the base; (5) `D200xDriver` render/press/dial against a fake HID; (6) `app.py` explicit deck kind `"d200x"`; (7) v1 Orchestrator dial routing (roaming selection cursor + drill/pager/brightness) + side-button Approve/Deny.
- **v2 (later phase):** on-deck multi-option answer + refined selection scrub.
- **Final task:** reconcile constants vs probe output + HW manual gate (post-purchase).
